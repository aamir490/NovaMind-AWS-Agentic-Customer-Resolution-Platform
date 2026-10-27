<#
.SYNOPSIS
  Adds or hardens one existing CloudFront /api/* behavior backed by a VPC Origin.
.DESCRIPTION
  Default mode performs local validation only. -Apply verifies the supplied VPC
  Origin is deployed, validates the reviewed origin-request policy, then makes
  one ETag-guarded distribution update. It never creates a distribution, DNS
  record, certificate, public ALB origin, secret, or VPC endpoint.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Z0-9]{1,64}$')] [string]$DistributionId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')] [string]$ApiOriginId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')] [string]$VpcOriginId,
    [Parameter(Mandatory)] [ValidatePattern('^arn:aws(?:-[a-z-]+)?:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:loadbalancer/app/[A-Za-z0-9-]+/[0-9a-f]+$')] [string]$InternalAlbArn,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')] [string]$InternalAlbDnsName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9-]{1,128}$')] [string]$CachingDisabledPolicyId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9-]{1,128}$')] [string]$AuthorizationOriginRequestPolicyId,
    [switch]$AddMissingApiRoute,
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function Set-JsonProperty {
    param([object]$Object, [string]$Name, $Value)
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Get-CloudFrontCollectionItems {
    param([object]$Collection, [string]$CollectionName)
    if ($null -eq $Collection -or $null -eq $Collection.PSObject.Properties['Quantity']) {
        throw "CloudFront $CollectionName lacks a Quantity field."
    }
    $quantity = [int]$Collection.Quantity
    if ($quantity -lt 0) { throw "CloudFront $CollectionName has an invalid Quantity." }
    $itemsProperty = $Collection.PSObject.Properties['Items']
    if ($null -eq $itemsProperty) {
        if ($quantity -eq 0) { return }
        throw "CloudFront $CollectionName has a nonzero Quantity but no Items array."
    }
    $items = @($itemsProperty.Value)
    if ($items.Count -ne $quantity) {
        throw "CloudFront $CollectionName Quantity does not match its Items array."
    }
    return $items
}

function Get-OptionalJsonProperty {
    param([object]$Object, [string]$Name)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function New-ApiBehavior {
    param([object]$DefaultBehavior, [string]$OriginId, [string]$CachePolicyId, [string]$RequestPolicyId)
    $behavior = $DefaultBehavior | ConvertTo-Json -Depth 100 | ConvertFrom-Json -ErrorAction Stop
    Set-JsonProperty $behavior 'PathPattern' '/api/*'
    Set-JsonProperty $behavior 'TargetOriginId' $OriginId
    Set-JsonProperty $behavior 'ViewerProtocolPolicy' 'redirect-to-https'
    Set-JsonProperty $behavior 'CachePolicyId' $CachePolicyId
    Set-JsonProperty $behavior 'OriginRequestPolicyId' $RequestPolicyId
    Set-JsonProperty $behavior 'AllowedMethods' ([pscustomobject]@{
        Quantity = 7
        Items = @('GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'POST', 'DELETE')
        CachedMethods = [pscustomobject]@{ Quantity = 2; Items = @('GET', 'HEAD') }
    })
    Set-JsonProperty $behavior 'Compress' $true
    foreach ($legacyProperty in @('ForwardedValues', 'MinTTL', 'DefaultTTL', 'MaxTTL')) {
        $behavior.PSObject.Properties.Remove($legacyProperty)
    }
    return $behavior
}

function Ensure-ApiRoute {
    param(
        [object]$DistributionConfig,
        [string]$OriginId,
        [string]$VpcOriginId,
        [string]$InternalAlbDnsName,
        [string]$CachePolicyId,
        [string]$RequestPolicyId
    )
    $allOrigins = @(Get-CloudFrontCollectionItems -Collection $DistributionConfig.Origins -CollectionName 'Origins')
    $allBehaviors = @(Get-CloudFrontCollectionItems -Collection $DistributionConfig.CacheBehaviors -CollectionName 'CacheBehaviors')
    $origins = @($allOrigins | Where-Object { [string]$_.Id -eq $OriginId })
    $behaviors = @($allBehaviors | Where-Object { [string]$_.PathPattern -eq '/api/*' })
    if ($origins.Count -gt 1 -or $behaviors.Count -gt 1 -or (($origins.Count -eq 0) -ne ($behaviors.Count -eq 0))) {
        throw 'Refusing to alter CloudFront: the API origin and /api/* behavior must be absent together or present exactly once.'
    }
    if ($origins.Count -eq 0) {
        $origin = [pscustomobject]@{
            Id = $OriginId
            DomainName = $InternalAlbDnsName
            OriginPath = ''
            CustomHeaders = [pscustomobject]@{ Quantity = 0 }
            VpcOriginConfig = [pscustomobject]@{ VpcOriginId = $VpcOriginId; OriginReadTimeout = 30; OriginKeepaliveTimeout = 5 }
            ConnectionAttempts = 3
            ConnectionTimeout = 10
            OriginShield = [pscustomobject]@{ Enabled = $false }
        }
        Set-JsonProperty $DistributionConfig.Origins 'Items' (@($allOrigins) + $origin)
        Set-JsonProperty $DistributionConfig.Origins 'Quantity' (@($allOrigins).Count + 1)
        $behavior = New-ApiBehavior -DefaultBehavior $DistributionConfig.DefaultCacheBehavior -OriginId $OriginId -CachePolicyId $CachePolicyId -RequestPolicyId $RequestPolicyId
        Set-JsonProperty $DistributionConfig.CacheBehaviors 'Items' (@($allBehaviors) + $behavior)
        Set-JsonProperty $DistributionConfig.CacheBehaviors 'Quantity' (@($allBehaviors).Count + 1)
        return
    }
    $origin = $origins[0]
    $customOriginConfig = Get-OptionalJsonProperty -Object $origin -Name 'CustomOriginConfig'
    $s3OriginConfig = Get-OptionalJsonProperty -Object $origin -Name 'S3OriginConfig'
    if ($null -eq $origin.VpcOriginConfig -or $origin.VpcOriginConfig.VpcOriginId -ne $VpcOriginId -or
        $origin.DomainName -ne $InternalAlbDnsName -or $null -ne $customOriginConfig -or $null -ne $s3OriginConfig) {
        throw 'The existing API origin is not the reviewed VPC Origin. Refusing type conversion or implicit origin replacement.'
    }
    $behavior = $behaviors[0]
    $hardened = New-ApiBehavior -DefaultBehavior $behavior -OriginId $OriginId -CachePolicyId $CachePolicyId -RequestPolicyId $RequestPolicyId
    foreach ($property in $hardened.PSObject.Properties) { Set-JsonProperty $behavior $property.Name $property.Value }
}

if (-not $Apply) {
    $action = if ($AddMissingApiRoute) { 'add or harden' } else { 'harden an existing' }
    Write-Output "Plan only: would $action CloudFront $DistributionId /api/* behavior using VPC Origin $VpcOriginId."
    Write-Output 'No AWS, CloudFront, ALB, API, database, Docker, or secret request was made.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }

$vpcOriginRaw = (& aws --profile $ProfileName cloudfront get-vpc-origin --id $VpcOriginId --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the supplied CloudFront VPC Origin.' }
try { $vpcOrigin = $vpcOriginRaw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'CloudFront VPC Origin response was invalid.' }
if ($vpcOrigin.VpcOrigin.Id -ne $VpcOriginId -or $vpcOrigin.VpcOrigin.Status -ne 'Deployed' -or
    [string]$vpcOrigin.VpcOrigin.VpcOriginEndpointConfig.Arn -ne $InternalAlbArn) {
    throw 'The supplied CloudFront VPC Origin is not deployed for the reviewed internal ALB; do not update the distribution yet.'
}

$policyRaw = (& aws --profile $ProfileName cloudfront get-origin-request-policy --id $AuthorizationOriginRequestPolicyId `
    --query 'OriginRequestPolicy.OriginRequestPolicyConfig.{HeaderBehavior:HeadersConfig.HeaderBehavior,Headers:HeadersConfig.Headers.Items,QueryStringBehavior:QueryStringsConfig.QueryStringBehavior}' `
    --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the supplied CloudFront origin request policy.' }
try { $policy = $policyRaw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'CloudFront origin request policy was invalid.' }
$headerBehavior = [string]$policy.HeaderBehavior
$listedAuthorization = @($policy.Headers | Where-Object { [string]$_ -ieq 'Authorization' }).Count -eq 1
$forwardsAllHeaders = $headerBehavior -in @('allViewer', 'allViewerAndWhitelistCloudFront')
$forwardsAllExceptListed = $headerBehavior -in @('allExcept', 'allViewerExcept') -and -not $listedAuthorization
$forwardsAuthorization = $forwardsAllHeaders -or $forwardsAllExceptListed -or ($headerBehavior -eq 'whitelist' -and $listedAuthorization)
if (-not $forwardsAuthorization -or [string]$policy.QueryStringBehavior -ne 'all') {
    throw 'The supplied origin request policy must forward Authorization and all query strings to the API origin.'
}

$raw = (& aws --profile $ProfileName cloudfront get-distribution-config --id $DistributionId --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the CloudFront distribution configuration.' }
try { $document = $raw | ConvertFrom-Json -ErrorAction Stop } catch { throw 'CloudFront distribution configuration was invalid.' }
if (-not $document.ETag -or $null -eq $document.DistributionConfig -or $null -eq $document.DistributionConfig.DefaultCacheBehavior) {
    throw 'CloudFront response lacks an ETag or required distribution configuration.'
}

$existingApiOrigins = @(Get-CloudFrontCollectionItems -Collection $document.DistributionConfig.Origins -CollectionName 'Origins' | Where-Object { [string]$_.Id -eq $ApiOriginId })
$existingApiBehaviors = @(Get-CloudFrontCollectionItems -Collection $document.DistributionConfig.CacheBehaviors -CollectionName 'CacheBehaviors' | Where-Object { [string]$_.PathPattern -eq '/api/*' })
if (($existingApiOrigins.Count -eq 0) -and ($existingApiBehaviors.Count -eq 0) -and -not $AddMissingApiRoute) {
    throw 'The API origin and /api/* behavior are absent. Re-run with -AddMissingApiRoute only after review.'
}
Ensure-ApiRoute -DistributionConfig $document.DistributionConfig -OriginId $ApiOriginId -VpcOriginId $VpcOriginId `
    -InternalAlbDnsName $InternalAlbDnsName -CachePolicyId $CachingDisabledPolicyId -RequestPolicyId $AuthorizationOriginRequestPolicyId

$temporaryConfig = New-TemporaryFile
try {
    [System.IO.File]::WriteAllText($temporaryConfig, ($document.DistributionConfig | ConvertTo-Json -Depth 100), [System.Text.UTF8Encoding]::new($false))
    & aws --profile $ProfileName cloudfront update-distribution --id $DistributionId --if-match $document.ETag `
        --distribution-config ("file://{0}" -f $temporaryConfig) --no-cli-pager 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'CloudFront rejected the VPC-Origin update. The ETag precondition prevented an unsafe overwrite if the distribution changed.' }
} finally {
    Remove-Item -LiteralPath $temporaryConfig -Force -ErrorAction SilentlyContinue
}
Write-Output 'CloudFront VPC-Origin update accepted. Wait for distribution deployment, then run the release verifier.'
