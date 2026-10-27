<#
.SYNOPSIS
  Creates a review-only CloudFormation change set for a NovaMind template.
.DESCRIPTION
  Default mode validates checked-in JSON locally. -Apply creates but never
  executes a change set, so Kiro can inspect exact additions before any stack
  mutation. Parameter files contain identifiers only; do not put a database
  password, bearer token, or secret value in them.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z][A-Za-z0-9-]{0,127}$')] [string]$StackName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z][A-Za-z0-9-]{0,127}$')] [string]$ChangeSetName,
    [Parameter(Mandatory)] [ValidateSet('CREATE', 'UPDATE')] [string]$ChangeSetType,
    [Parameter(Mandatory)] [string]$TemplatePath,
    [Parameter(Mandatory)] [string]$ParametersPath,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

foreach ($path in @($TemplatePath, $ParametersPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Required file is missing: $path" }
}
try {
    $template = Get-Content -Raw -LiteralPath $TemplatePath | ConvertFrom-Json -ErrorAction Stop
    $parameters = Get-Content -Raw -LiteralPath $ParametersPath | ConvertFrom-Json -ErrorAction Stop
} catch {
    throw 'CloudFormation template and parameter files must be valid JSON.'
}
if (-not $template.Resources -or -not ($parameters -is [System.Collections.IEnumerable])) {
    throw 'Template must contain resources and parameters must be a JSON array.'
}
foreach ($parameter in @($parameters)) {
    if ($null -eq $parameter.ParameterKey -or $null -eq $parameter.ParameterValue -or
        $parameter.PSObject.Properties.Name -notcontains 'ParameterKey' -or
        $parameter.PSObject.Properties.Name -notcontains 'ParameterValue') {
        throw 'Each parameter must contain ParameterKey and ParameterValue.'
    }
}

# Creation-mode parameters must not carry fake resource IDs. CloudFormation
# validates typed parameters before evaluating Conditions, so an inactive
# sg-00000000/subnet-00000000 value can fail a stack before the intended
# resources are considered. Empty values are allowed only when the matching
# Create* switch is true; otherwise a real ID is required.
$parameterMap = @{}
foreach ($parameter in @($parameters)) {
    $key = [string]$parameter.ParameterKey
    if ($parameterMap.ContainsKey($key)) { throw "Duplicate CloudFormation parameter: $key" }
    $parameterMap[$key] = [string]$parameter.ParameterValue
}
function Assert-NetworkExistingResourceParameter {
    param([string]$CreateParameter, [string]$ExistingParameter, [string]$Label)
    if (-not $parameterMap.ContainsKey($CreateParameter) -or -not $parameterMap.ContainsKey($ExistingParameter)) { return }
    $create = $parameterMap[$CreateParameter]
    $existing = $parameterMap[$ExistingParameter]
    $placeholder = $existing -in @('sg-00000000', 'subnet-00000000')
    if ($create -eq 'true') {
        if (-not [string]::IsNullOrWhiteSpace($existing)) { throw "$ExistingParameter must be empty when $CreateParameter=true; inactive resource IDs are forbidden." }
    } elseif ([string]::IsNullOrWhiteSpace($existing) -or $placeholder) {
        throw "$ExistingParameter must contain a real $Label when $CreateParameter=false."
    }
}
Assert-NetworkExistingResourceParameter 'CreateOriginPrivateSubnets' 'ExistingOriginPrivateSubnetAId' 'origin subnet ID'
Assert-NetworkExistingResourceParameter 'CreateOriginPrivateSubnets' 'ExistingOriginPrivateSubnetBId' 'origin subnet ID'
Assert-NetworkExistingResourceParameter 'CreateLoadBalancerSecurityGroup' 'ExistingLoadBalancerSecurityGroupId' 'load balancer security-group ID'
Assert-NetworkExistingResourceParameter 'CreateApiSecurityGroup' 'ExistingApiSecurityGroupId' 'API security-group ID'
if (-not $Apply) {
    Write-Output "Plan only: would create a $ChangeSetType change set $ChangeSetName for stack $StackName."
    Write-Output 'No AWS API call or CloudFormation change set was created. Review parameter values locally without printing sensitive material.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }
& aws --profile $ProfileName --region $Region cloudformation create-change-set --stack-name $StackName `
    --change-set-name $ChangeSetName --change-set-type $ChangeSetType `
    --template-body ("file://{0}" -f (Resolve-Path -LiteralPath $TemplatePath)) `
    --parameters ("file://{0}" -f (Resolve-Path -LiteralPath $ParametersPath)) `
    --capabilities CAPABILITY_NAMED_IAM --no-cli-pager 2>$null
if ($LASTEXITCODE -ne 0) { throw 'CloudFormation rejected the change-set request.' }
Write-Output "Change set created but not executed: $StackName/$ChangeSetName"
Write-Output "Review: aws --profile $ProfileName --region $Region cloudformation describe-change-set --stack-name $StackName --change-set-name $ChangeSetName --no-cli-pager"
Write-Output "Execute only after approval: aws --profile $ProfileName --region $Region cloudformation execute-change-set --stack-name $StackName --change-set-name $ChangeSetName --no-cli-pager"
