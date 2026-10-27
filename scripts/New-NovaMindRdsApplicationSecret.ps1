<#
.SYNOPSIS
  Creates the seed application secret for the one-time NovaMind RDS bootstrap.
.DESCRIPTION
  Default mode validates only local inputs. -Apply creates a new secret with a
  Secrets Manager-generated password; it never reads an existing secret and
  never prints its value. The bootstrap ECS task later adds the RDS endpoint,
  schema, and TLS connection material to this same secret.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateNotNullOrEmpty()]
    [string]$RdsCaBundlePath,
    [ValidatePattern('^[A-Za-z0-9/_+=.@-]{1,512}$')]
    [string]$SecretName = 'novamind-crp-postgres-app-dev',
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')]
    [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()]
    [string]$ProfileName = 'novamind',
    [ValidatePattern('^$|^arn:aws(?:-[a-z-]+)?:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+$')]
    [string]$KmsKeyArn = '',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'
$trustedCaBundlePath = '/etc/novamind/rds-ca-bundle.pem'

function Assert-RdsCaBundle {
    param([Parameter(Mandatory)] [string]$Certificate)

    $certificateBytes = [System.Text.Encoding]::UTF8.GetByteCount($Certificate)
    if ($Certificate.Length -lt 128 -or $certificateBytes -gt 1MB -or
        -not $Certificate.Contains('-----BEGIN CERTIFICATE-----') -or
        -not $Certificate.Contains('-----END CERTIFICATE-----')) {
        throw 'RdsCaBundlePath is not a valid-looking PEM certificate bundle.'
    }
}

function Invoke-AwsCliCapture {
    param([Parameter(Mandatory)] [string[]]$Arguments)

    $stderrFile = New-TemporaryFile
    try {
        # Windows PowerShell promotes native stderr into NativeCommandError
        # under ErrorActionPreference=Stop. Capture it without terminating,
        # then return only sanitized diagnostics to the caller.
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $output = (& aws @Arguments 2>$stderrFile | Out-String)
            if ($null -eq $output) { $output = '' } else { $output = [string]$output }
            $output = $output.Trim()
            $exitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        $rawError = if (Test-Path $stderrFile) { Get-Content -Raw $stderrFile } else { '' }
        if ($null -eq $rawError) { $rawError = '' } else { $rawError = [string]$rawError }
        $rawError = $rawError.Trim()
        $safeError = $rawError -replace '(?i)(password|SecretString|rds_ca_pem|BEGIN CERTIFICATE)[^,}]]*', '<redacted>'
        return [pscustomobject]@{ Output = $output; ExitCode = $exitCode; Error = $safeError }
    } finally {
        Remove-Item -LiteralPath $stderrFile -Force -ErrorAction SilentlyContinue
    }
}

function Protect-SecretPayloadFile {
    param([Parameter(Mandatory)] [string]$Path)

    $acl = Get-Acl -LiteralPath $Path
    # Preserve the inherited temporary-directory entries while converting them
    # to explicit rules. Removing them outright can revoke the current
    # sandboxed process's write access before the payload is written.
    $acl.SetAccessRuleProtection($true, $true)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $fullControl = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $identity, 'FullControl', 'Allow')
    [void]$acl.SetAccessRule($fullControl)
    Set-Acl -LiteralPath $Path -AclObject $acl
}

if (-not $Apply) {
    Write-Output "Plan only: would create new Secrets Manager secret '$SecretName' in $Region."
    Write-Output 'No AWS API call, secret read, or credential generation was performed. Re-run with -Apply after reviewing the CA bundle.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    throw 'AWS CLI is required for -Apply.'
}
if (-not (Test-Path -LiteralPath $RdsCaBundlePath -PathType Leaf)) {
    throw 'RdsCaBundlePath must identify the reviewed AWS RDS CA bundle.'
}

$certificate = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $RdsCaBundlePath), [System.Text.Encoding]::UTF8)
Assert-RdsCaBundle -Certificate $certificate

$passwordArguments = @(
    '--profile', $ProfileName, '--region', $Region,
    'secretsmanager', 'get-random-password',
    '--password-length', '48',
    '--exclude-characters', '"''\/ ',
    '--require-each-included-type',
    '--query', 'RandomPassword', '--output', 'text', '--no-cli-pager'
)
$passwordResult = Invoke-AwsCliCapture -Arguments $passwordArguments
if ($passwordResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($passwordResult.Output)) {
    throw "Secrets Manager refused password generation (exit $($passwordResult.ExitCode)). Sanitised detail: $($passwordResult.Error)"
}
$randomPassword = $passwordResult.Output.Trim()
if ($randomPassword.Length -ne 48 -or
    $randomPassword -match '\s' -or
    $randomPassword.IndexOf([char]0) -ge 0) {
    throw 'Secrets Manager returned an invalid application password; refusing to create the seed secret.'
}

# The generated password is held only in memory and in this ACL-restricted
# temporary payload file. The reviewed CA bundle remains a separate trusted
# file baked into the bootstrap/runtime image; only its fixed path is stored.
# The password is never a command-line argument or console value.
$payload = [ordered]@{
    username = 'novamind_app'
    password = $randomPassword
    rds_ca_path = $trustedCaBundlePath
} | ConvertTo-Json -Compress -Depth 3
$temporaryPayloadFile = New-TemporaryFile
try {
    Protect-SecretPayloadFile -Path $temporaryPayloadFile
    [System.IO.File]::WriteAllText($temporaryPayloadFile, $payload, [System.Text.UTF8Encoding]::new($false))
    $createArguments = @(
        '--profile', $ProfileName, '--region', $Region,
        'secretsmanager', 'create-secret', '--name', $SecretName,
        '--description', 'NovaMind application PostgreSQL credentials; initialized by one-time ECS bootstrap.',
        '--secret-string', "file://$temporaryPayloadFile",
        '--query', 'ARN', '--output', 'text', '--no-cli-pager'
    )
    if ($KmsKeyArn) { $createArguments += @('--kms-key-id', $KmsKeyArn) }
    $createResult = Invoke-AwsCliCapture -Arguments $createArguments
    if ($createResult.ExitCode -ne 0 -or $createResult.Output -notmatch '^arn:aws(?:-[a-z-]+)?:secretsmanager:') {
        if ($createResult.Error -match 'ResourceExistsException') {
            throw 'Secrets Manager refused secret creation: a secret with this name already exists. Do not overwrite it; use its existing ARN.'
        } elseif ($createResult.Error -match 'AccessDenied|AuthFailure|InvalidClientTokenId') {
            throw 'Secrets Manager refused secret creation: IAM permission denied. Verify the profile has secretsmanager:CreateSecret.'
        } elseif ($createResult.Error -match 'InvalidParameterException') {
            throw "Secrets Manager refused secret creation: invalid parameter. Sanitised detail: $($createResult.Error)"
        } else {
            throw "Secrets Manager refused secret creation (exit $($createResult.ExitCode)). Sanitised detail: $($createResult.Error)"
        }
    }
    $secretArn = $createResult.Output
} finally {
    Remove-Item -LiteralPath $temporaryPayloadFile -Force -ErrorAction SilentlyContinue
}
Write-Output "Application seed secret created. ARN: $secretArn"
