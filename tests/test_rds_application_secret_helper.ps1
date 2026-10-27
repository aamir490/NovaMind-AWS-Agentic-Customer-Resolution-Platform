# Offline contract test for the RDS application-secret creation helper.
# This test reads the script only; it never invokes AWS or reads a secret.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = Split-Path -Parent $PSScriptRoot
$helperPath = Join-Path $workspace 'scripts/New-NovaMindRdsApplicationSecret.ps1'
$source = Get-Content -LiteralPath $helperPath -Raw

$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $helperPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw 'Application-secret helper did not parse.' }
$validationFunction = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-RdsCaBundle'
}, $true)
if ($null -eq $validationFunction) { throw 'CA-bundle validation function was not found.' }
. ([scriptblock]::Create($validationFunction.Extent.Text))

$largeValidBundle = ('x' * 131200) + "`n-----BEGIN CERTIFICATE-----`n-----END CERTIFICATE-----`n"
Assert-RdsCaBundle -Certificate $largeValidBundle
$tooLargeBundle = ('x' * (1MB + 1)) + "`n-----BEGIN CERTIFICATE-----`n-----END CERTIFICATE-----`n"
$rejected = $false
try { Assert-RdsCaBundle -Certificate $tooLargeBundle } catch { $rejected = $true }
if (-not $rejected) { throw 'CA bundle above the 1 MiB maximum was accepted.' }

if ($source -notmatch '\$SecretName\s*=\s*''novamind-crp-postgres-app-dev''') {
    throw 'The application-secret helper default name is not the reviewed project-specific name.'
}
foreach ($required in @(
    "'secretsmanager', 'get-random-password'",
    "'secretsmanager', 'create-secret'",
    "'--secret-string'",
    "'--password-length', '48'",
    'BEGIN CERTIFICATE',
    'ResourceExistsException',
    'SetAccessRuleProtection',
    'FileSystemAccessRule',
    'Remove-Item -LiteralPath $temporaryPayloadFile',
    'rds_ca_path',
    'never reads an existing secret'
)) {
    if ($source -notmatch [regex]::Escape($required)) {
        throw "Missing application-secret safety contract: $required"
    }
}
if ($source -notmatch '\$randomPassword\.Length -ne 48' -or
    $source -notmatch '\$randomPassword -match ''\\s''') {
    throw 'The helper must reject malformed or whitespace-contaminated generated passwords before secret creation.'
}
if ($source -match '--generate-secret-string') {
    throw 'Unsupported --generate-secret-string option remains in helper.'
}
if ($source -match 'rds_ca_pem\s*=') {
    throw 'The helper must not place the CA PEM in the Secrets Manager payload.'
}
$payloadSizeProbe = [ordered]@{
    username = 'novamind_app'
    password = ('P' * 48)
    rds_ca_path = '/etc/novamind/rds-ca-bundle.pem'
} | ConvertTo-Json -Compress
if ([Text.Encoding]::UTF8.GetByteCount($payloadSizeProbe) -ge 65536) {
    throw 'Credential seed payload exceeds the Secrets Manager size limit.'
}
foreach ($forbidden in @('novamind/groq-api-key', 'novamind/auth-credentials')) {
    if ($source -match [regex]::Escape($forbidden)) {
        throw "Protected legacy secret name found in helper: $forbidden"
    }
}

$fakeRoot = Join-Path ([IO.Path]::GetTempPath()) ('novamind-rds-secret-test-' + [guid]::NewGuid().ToString('N'))
$caFile = Join-Path $fakeRoot 'ca.pem'
$fakeAws = Join-Path $fakeRoot 'aws.cmd'
$oldPath = $env:PATH
try {
    New-Item -ItemType Directory -Path $fakeRoot -Force | Out-Null
    @(
        '@echo off',
        'if "%~6"=="get-random-password" (',
        '  echo Abcdefghijklmnopqrstuvwxyz0123456789ABCDEF123456',
        '  exit /b 0',
        ')',
        'echo An error occurred (ResourceExistsException) 1>&2',
        'exit /b 254'
    ) | Set-Content -LiteralPath $fakeAws -Encoding ASCII
    ('-----BEGIN CERTIFICATE-----' + ('x' * 128) + '-----END CERTIFICATE-----') |
        Set-Content -LiteralPath $caFile -Encoding ASCII
    $env:PATH = "$fakeRoot;$oldPath"
    $caught = $null
    try {
        & $helperPath -RdsCaBundlePath $caFile -ProfileName novamind -Region us-east-1 -Apply 2>&1 | Out-Null
    } catch {
        $caught = $_.Exception.Message
    }
    if ($caught -notmatch 'secret with this name already exists') {
        throw "Native stderr regression was not classified safely: $caught"
    }
} finally {
    $env:PATH = $oldPath
    Remove-Item -LiteralPath $fakeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Output 'RDS_APPLICATION_SECRET_HELPER_TEST_PASS'
