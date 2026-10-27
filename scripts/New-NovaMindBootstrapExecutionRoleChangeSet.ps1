<##
.SYNOPSIS
  Prepares a review-only update Change Set for the bootstrap execution role.
.DESCRIPTION
  Windows PowerShell 5.1-compatible workflow. Existing deployed parameters are
  supplied as a reviewed JSON snapshot and preserved individually with
  UsePreviousValue=true. Only the execution-role parameters are overridden.
  Default mode performs local validation only; -Apply creates but never
  executes the Change Set.
##>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z][A-Za-z0-9-]{0,127}$')] [string]$StackName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z][A-Za-z0-9-]{0,127}$')] [string]$ChangeSetName,
    [Parameter(Mandatory)] [ValidateScript({ if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) { throw 'Existing parameter snapshot was not found.' }; $true })]
    [string]$ExistingParametersPath,
    [Parameter(Mandatory)] [ValidatePattern('^arn:aws(?:-[a-z-]+)?:ecr:[a-z0-9-]+:[0-9]{12}:repository/[A-Za-z0-9._/-]+$')]
    [string]$BootstrapEcrRepositoryArn,
    [ValidateScript({ if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) { throw 'Bootstrap support template was not found.' }; $true })]
    [string]$TemplatePath = (Join-Path $PSScriptRoot '..\infrastructure\novamind-rds-bootstrap-support.template.json'),
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function ConvertTo-ParameterObjects {
    param([Parameter(Mandatory)] [string]$Json)

    $trimmedJson = $Json.Trim()
    $arrayRoot = $trimmedJson.StartsWith('[') -and $trimmedJson.EndsWith(']')
    $wrappedRoot = $trimmedJson.StartsWith('{') -and $trimmedJson.EndsWith('}')
    if (-not $arrayRoot -and -not $wrappedRoot) {
        throw 'Existing parameter JSON must have an array or Parameters-object root.'
    }

    try {
        $root = $Json | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'Existing parameter JSON is invalid.'
    }

    # ConvertFrom-Json unwraps one-element arrays in Windows PowerShell 5.1.
    # @() normalizes both one-item and multi-item roots with built-in JSON support.
    if ($wrappedRoot -and ($null -ne $root) -and $root.PSObject.Properties.Name -contains 'Parameters') {
        $items = @($root.Parameters)
    } elseif ($arrayRoot) {
        $items = @($root)
    } else {
        throw 'Existing parameter JSON must contain a JSON array of parameters.'
    }
    if ($items.Count -eq 0) { throw 'Existing parameter JSON must contain a non-empty array.' }

    $result = @()
    $seen = @{}
    foreach ($item in $items) {
        if ($null -eq $item -or
            $item.PSObject.Properties.Name -notcontains 'ParameterKey' -or
            [string]::IsNullOrWhiteSpace([string]$item.ParameterKey)) {
            throw 'Each existing parameter must contain an individual ParameterKey.'
        }
        $key = [string]$item.ParameterKey
        if ($seen.ContainsKey($key)) { throw "Duplicate existing parameter key: $key" }
        $seen[$key] = $true
        $result += [pscustomobject]@{ ParameterKey = $key; UsePreviousValue = $true }
    }
    return @($result)
}

function Merge-BootstrapExecutionParameters {
    param(
        [Parameter(Mandatory)] [object[]]$Existing,
        [Parameter(Mandatory)] [string]$EcrRepositoryArn
    )

    $overrides = [ordered]@{
        CreateBootstrapExecutionRole = 'true'
        BootstrapExecutionRoleName = 'novamind-crp-bootstrap-execution-dev'
        ExistingBootstrapExecutionRoleName = ''
        BootstrapEcrRepositoryArn = $EcrRepositoryArn
    }
    $merged = @($Existing | ForEach-Object {
        [pscustomobject]@{ ParameterKey = [string]$_.ParameterKey; UsePreviousValue = $true }
    })
    foreach ($entry in $overrides.GetEnumerator()) {
        $match = @($merged | Where-Object { $_.ParameterKey -ceq $entry.Key })
        if ($match.Count -eq 1) {
            $merged = @($merged | Where-Object { $_.ParameterKey -cne $entry.Key })
        }
        $merged += [pscustomobject]@{
            ParameterKey = $entry.Key
            ParameterValue = $entry.Value
        }
    }
    return @($merged)
}

function Assert-TemplateContract {
    param([Parameter(Mandatory)] [string]$Path)
    try {
        $template = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw 'Bootstrap support template is invalid JSON.'
    }
    if ($null -eq $template.Resources.BootstrapExecutionRole -or
        $null -eq $template.Parameters.BootstrapEcrRepositoryArn) {
        throw 'Bootstrap support template lacks the execution-role contract.'
    }
}

Assert-TemplateContract -Path $TemplatePath
$existingJson = Get-Content -Raw -LiteralPath $ExistingParametersPath
$existing = ConvertTo-ParameterObjects -Json $existingJson
$finalParameters = Merge-BootstrapExecutionParameters -Existing $existing -EcrRepositoryArn $BootstrapEcrRepositoryArn

# This diagnostic is deliberately metadata-only: it never prints parameter values.
Write-Output "PARAMETER_JSON_COUNT=$($finalParameters.Count)"
Write-Output ("PARAMETER_JSON_KEYS=" + (($finalParameters | ForEach-Object { $_.ParameterKey }) -join '|'))
Write-Output "PARAMETER_USE_PREVIOUS_COUNT=$(@($finalParameters | Where-Object { $_.PSObject.Properties.Name -contains 'UsePreviousValue' -and $_.UsePreviousValue -eq $true }).Count)"

if (-not $Apply) {
    Write-Output "Plan only: would create UPDATE change set $ChangeSetName for stack $StackName."
    Write-Output 'Existing parameters remain UsePreviousValue=true; only the four bootstrap execution-role parameters change.'
    Write-Output 'No AWS API call or CloudFormation change set was created.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }

$parameterFile = New-TemporaryFile
try {
    [System.IO.File]::WriteAllText(
        $parameterFile,
        ($finalParameters | ConvertTo-Json -Depth 10),
        [System.Text.UTF8Encoding]::new($false))
    & aws --profile $ProfileName --region $Region cloudformation create-change-set `
        --stack-name $StackName --change-set-name $ChangeSetName --change-set-type UPDATE `
        --template-body ("file://{0}" -f (Resolve-Path -LiteralPath $TemplatePath)) `
        --parameters ("file://{0}" -f $parameterFile) `
        --capabilities CAPABILITY_NAMED_IAM --no-cli-pager 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'CloudFormation rejected the bootstrap execution-role Change Set request.' }
} finally {
    Remove-Item -LiteralPath $parameterFile -Force -ErrorAction SilentlyContinue
}
Write-Output "Change set created but not executed: $StackName/$ChangeSetName"
