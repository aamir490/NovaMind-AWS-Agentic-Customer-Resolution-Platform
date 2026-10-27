$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $root 'scripts/New-NovaMindCloudFormationChangeSet.ps1'
$template = New-TemporaryFile
$parameters = New-TemporaryFile
try {
    [IO.File]::WriteAllText($template, '{"Resources":{"Marker":{"Type":"AWS::SNS::Topic"}}}', [Text.UTF8Encoding]::new($false))
    function Invoke-Plan([string]$json) {
        [IO.File]::WriteAllText($parameters, $json, [Text.UTF8Encoding]::new($false))
        return @(& $helper -StackName novamind-crp-api-vpc-origin-dev -ChangeSetName test-network -ChangeSetType CREATE -TemplatePath $template -ParametersPath $parameters 2>&1)
    }
    $creation = Invoke-Plan '[{"ParameterKey":"CreateOriginPrivateSubnets","ParameterValue":"true"},{"ParameterKey":"CreateLoadBalancerSecurityGroup","ParameterValue":"true"},{"ParameterKey":"CreateApiSecurityGroup","ParameterValue":"false"},{"ParameterKey":"ExistingApiSecurityGroupId","ParameterValue":"sg-0726ae466e736b514"}]'
    if (($creation -join "`n") -notmatch 'Plan only:') { throw 'Creation-mode parameters were not accepted.' }

    $placeholderRejected = $false
    try { Invoke-Plan '[{"ParameterKey":"CreateOriginPrivateSubnets","ParameterValue":"true"},{"ParameterKey":"ExistingOriginPrivateSubnetAId","ParameterValue":"subnet-00000000"}]' | Out-Null } catch { $placeholderRejected = $true }
    if (-not $placeholderRejected) { throw 'Placeholder subnet ID was accepted in creation mode.' }

    $missingExistingRejected = $false
    try { Invoke-Plan '[{"ParameterKey":"CreateLoadBalancerSecurityGroup","ParameterValue":"false"},{"ParameterKey":"ExistingLoadBalancerSecurityGroupId","ParameterValue":""}]' | Out-Null } catch { $missingExistingRejected = $true }
    if (-not $missingExistingRejected) { throw 'Missing existing security-group ID was accepted when creation was disabled.' }
    Write-Output 'NETWORK_CHANGESET_PARAMETER_TEST_PASS'
} finally {
    Remove-Item -LiteralPath $template,$parameters -Force -ErrorAction SilentlyContinue
}
