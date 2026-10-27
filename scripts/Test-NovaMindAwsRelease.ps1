<#
.SYNOPSIS
  Verifies a non-secret NovaMind ECS/internal-ALB/CloudFront-VPC-Origin release.
.DESCRIPTION
  Default mode is local validation only. -Apply reads ECS, ELB, and CloudFront
  metadata and requests the public /api/health endpoint. It never retrieves a
  secret, authenticates a user, invokes Bedrock, or mutates AWS resources.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,255}$')] [string]$ClusterName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,255}$')] [string]$ServiceName,
    [Parameter(Mandatory)] [ValidatePattern('^arn:aws(?:-[a-z-]+)?:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:targetgroup/[A-Za-z0-9-]+/[0-9a-f]+$')] [string]$TargetGroupArn,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Z0-9]{1,64}$')] [string]$DistributionId,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+/?$')] [string]$CloudFrontUrl,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')] [string]$ApiOriginId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')] [string]$VpcOriginId,
    [Parameter(Mandatory)] [ValidatePattern('^arn:aws(?:-[a-z-]+)?:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/app/[A-Za-z0-9-]+/[0-9a-f]+$')] [string]$ExpectedInternalAlbArn,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')] [string]$ExpectedInternalAlbDnsName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9-]{1,128}$')] [string]$CachingDisabledPolicyId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9-]{1,128}$')] [string]$AuthorizationOriginRequestPolicyId,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function Test-AuthorizationPolicy {
    param([string]$PolicyId)
    $originPolicy = (& aws --profile $ProfileName cloudfront get-origin-request-policy --id $PolicyId `
        --query 'OriginRequestPolicy.OriginRequestPolicyConfig.{HeaderBehavior:HeadersConfig.HeaderBehavior,Headers:HeadersConfig.Headers.Items,QueryStringBehavior:QueryStringsConfig.QueryStringBehavior}' `
        --output json --no-cli-pager 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the CloudFront API origin request policy.' }
    try { $originPolicyInfo = $originPolicy | ConvertFrom-Json -ErrorAction Stop } catch { throw 'CloudFront API origin request policy was invalid.' }
    $headerBehavior = [string]$originPolicyInfo.HeaderBehavior
    $listedAuthorization = @($originPolicyInfo.Headers | Where-Object { [string]$_ -ieq 'Authorization' }).Count -eq 1
    $forwardsAllHeaders = $headerBehavior -in @('allViewer', 'allViewerAndWhitelistCloudFront')
    $forwardsAllExceptListed = $headerBehavior -in @('allExcept', 'allViewerExcept') -and -not $listedAuthorization
    $forwardsAuthorization = $forwardsAllHeaders -or $forwardsAllExceptListed -or ($headerBehavior -eq 'whitelist' -and $listedAuthorization)
    if (-not $forwardsAuthorization -or [string]$originPolicyInfo.QueryStringBehavior -ne 'all') {
        throw 'CloudFront API origin request policy does not forward Authorization and all query strings.'
    }
}

if (-not $Apply) {
    Write-Output "Plan only: would verify service $ClusterName/$ServiceName, target health, deployed VPC Origin $VpcOriginId, and $($CloudFrontUrl.TrimEnd('/'))/api/health."
    Write-Output 'No AWS, CloudFront, ALB, API, database, Bedrock, or Cognito request was made.'
    return
}
foreach ($command in 'aws', 'curl.exe') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "$command is required for -Apply." }
}
$service = (& aws --profile $ProfileName --region $Region ecs describe-services --cluster $ClusterName --services $ServiceName `
    --query 'services[0].{Status:status,Desired:desiredCount,Running:runningCount,Pending:pendingCount,TaskDefinition:taskDefinition}' `
    --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect ECS service.' }
$serviceInfo = $service | ConvertFrom-Json -ErrorAction Stop
if ($serviceInfo.Status -ne 'ACTIVE' -or $serviceInfo.Desired -lt 1 -or $serviceInfo.Running -ne $serviceInfo.Desired -or $serviceInfo.Pending -ne 0) {
    throw 'ECS service is not active and fully running.'
}
$targets = (& aws --profile $ProfileName --region $Region elbv2 describe-target-health --target-group-arn $TargetGroupArn `
    --query 'TargetHealthDescriptions[].TargetHealth.State' --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect ALB target health.' }
$targetStates = @($targets | ConvertFrom-Json -ErrorAction Stop | Where-Object { $null -ne $_ })
if ($targetStates.Count -lt 1 -or @($targetStates | Where-Object { $_ -ne 'healthy' }).Count -ne 0) {
    throw 'ALB does not report every registered API target healthy.'
}
$vpcOriginRaw = (& aws --profile $ProfileName cloudfront get-vpc-origin --id $VpcOriginId --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect CloudFront VPC Origin.' }
try { $vpcOrigin = $vpcOriginRaw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'CloudFront VPC Origin response was invalid.' }
if ($vpcOrigin.VpcOrigin.Id -ne $VpcOriginId -or $vpcOrigin.VpcOrigin.Status -ne 'Deployed' -or
    [string]$vpcOrigin.VpcOrigin.VpcOriginEndpointConfig.Arn -ne $ExpectedInternalAlbArn) {
    throw 'CloudFront VPC Origin is not deployed for the reviewed internal ALB.'
}
$distribution = (& aws --profile $ProfileName cloudfront get-distribution-config --id $DistributionId `
    --query 'DistributionConfig.{Enabled:Enabled,Origins:Origins.Items,ApiBehaviors:CacheBehaviors.Items[?PathPattern==`/api/*`].{Origin:TargetOriginId,Viewer:ViewerProtocolPolicy,CachePolicy:CachePolicyId,OriginRequestPolicy:OriginRequestPolicyId,AllowedMethods:AllowedMethods.Items,CachedMethods:AllowedMethods.CachedMethods.Items}}' `
    --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect CloudFront distribution configuration.' }
$distributionInfo = $distribution | ConvertFrom-Json -ErrorAction Stop
$apiBehaviors = @($distributionInfo.ApiBehaviors | Where-Object { $null -ne $_ })
if ($distributionInfo.Enabled -ne $true -or $apiBehaviors.Count -ne 1 -or
    $apiBehaviors[0].Viewer -ne 'redirect-to-https' -or
    $apiBehaviors[0].Origin -ne $ApiOriginId -or
    $apiBehaviors[0].CachePolicy -ne $CachingDisabledPolicyId -or
    $apiBehaviors[0].OriginRequestPolicy -ne $AuthorizationOriginRequestPolicyId -or
    @($apiBehaviors[0].AllowedMethods | Where-Object { $_ -notin @('GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'POST', 'DELETE') }).Count -ne 0 -or
    @($apiBehaviors[0].AllowedMethods).Count -ne 7 -or
    @($apiBehaviors[0].CachedMethods).Count -ne 2 -or
    @($apiBehaviors[0].CachedMethods | Where-Object { $_ -notin @('GET', 'HEAD') }).Count -ne 0) {
    throw 'CloudFront does not have the reviewed HTTPS viewer /api/* behavior, write methods, disabled cache policy, and Authorization-forwarding policy.'
}
$apiOrigins = @($distributionInfo.Origins | Where-Object { [string]$_.Id -eq $ApiOriginId })
if ($apiOrigins.Count -ne 1 -or $apiOrigins[0].DomainName -ne $ExpectedInternalAlbDnsName -or
    $apiOrigins[0].VpcOriginConfig.VpcOriginId -ne $VpcOriginId -or
    $null -ne $apiOrigins[0].CustomOriginConfig -or $null -ne $apiOrigins[0].S3OriginConfig) {
    throw 'CloudFront API origin is not pinned to the reviewed internal ALB VPC Origin.'
}
Test-AuthorizationPolicy -PolicyId $AuthorizationOriginRequestPolicyId
$health = (& curl.exe --fail --silent --show-error --max-time 10 --noproxy '*' "$($CloudFrontUrl.TrimEnd('/'))/api/health" 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $health -notmatch '"status"\s*:\s*"ok"') { throw 'CloudFront API health verification failed.' }
Write-Output "ECS: $service"
Write-Output "ALB target states: $targets"
Write-Output "CloudFront API behavior: $distribution"
Write-Output 'Health response verified. Complete the interactive Cognito customer/reviewer HITL exercise separately; this script intentionally never obtains a user token.'
