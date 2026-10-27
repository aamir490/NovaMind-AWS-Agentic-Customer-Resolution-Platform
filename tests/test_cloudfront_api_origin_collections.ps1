$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Load helper functions through its plan-only path; this makes no AWS request.
. (Join-Path $root 'scripts\Configure-NovaMindCloudFrontApiOrigin.ps1') `
    -DistributionId E1MBA2UCNSORUF `
    -ApiOriginId novamind-api-vpc `
    -VpcOriginId vo_CQwe8iMNtfNE8WUQdABSbX `
    -InternalAlbArn 'arn:aws:elasticloadbalancing:us-east-1:357001292388:loadbalancer/app/novamind-api-internal/235337f46476476b' `
    -InternalAlbDnsName 'internal-novamind-api-internal-2060870413.us-east-1.elb.amazonaws.com' `
    -CachingDisabledPolicyId 4135ea2d-6df8-44a3-9df3-4b5a84be39ad `
    -AuthorizationOriginRequestPolicyId b689b0a8-53d0-40ab-baf2-68738e2966ac `
    -AddMissingApiRoute

function Assert-Equal {
    param($Actual, $Expected, [string]$Message)
    if ($Actual -ne $Expected) { throw "$Message Expected '$Expected'; got '$Actual'." }
}

function New-TestDistributionConfig {
    param([string[]]$ExistingPathPatterns)
    $behaviors = @($ExistingPathPatterns | ForEach-Object {
        [pscustomobject]@{ PathPattern = $_; TargetOriginId = 'frontend-s3'; ViewerProtocolPolicy = 'redirect-to-https'; CachePolicyId = 'frontend-policy' }
    })
    $cacheBehaviors = [pscustomobject]@{ Quantity = $behaviors.Count }
    if ($behaviors.Count -gt 0) { $cacheBehaviors | Add-Member -NotePropertyName Items -NotePropertyValue $behaviors }
    return [pscustomobject]@{
        Origins = [pscustomobject]@{ Quantity = 1; Items = @([pscustomobject]@{ Id = 'frontend-s3'; DomainName = 'frontend.example.test' }) }
        CacheBehaviors = $cacheBehaviors
        DefaultCacheBehavior = [pscustomobject]@{ TargetOriginId = 'frontend-s3'; ViewerProtocolPolicy = 'redirect-to-https'; CachePolicyId = 'frontend-policy'; Compress = $true }
    }
}

function Assert-ApiRoute {
    param([object]$Config, [int]$ExpectedExistingBehaviorCount)
    Ensure-ApiRoute -DistributionConfig $Config -OriginId novamind-api-vpc -VpcOriginId vo_test `
        -InternalAlbDnsName 'internal.example.test' -CachePolicyId disabled-policy -RequestPolicyId auth-policy
    Assert-Equal $Config.Origins.Quantity 2 'API origin must append without replacing S3.'
    Assert-Equal $Config.CacheBehaviors.Quantity ($ExpectedExistingBehaviorCount + 1) 'API behavior quantity must be exact.'
    Assert-Equal @($Config.CacheBehaviors.Items | Where-Object PathPattern -eq '/api/*').Count 1 'Exactly one API behavior is required.'
    Assert-Equal @($Config.CacheBehaviors.Items | Where-Object TargetOriginId -eq 'frontend-s3').Count $ExpectedExistingBehaviorCount 'Existing behaviors must be preserved.'

    # Reapplying the in-memory transformation must harden, not duplicate, the API route.
    Ensure-ApiRoute -DistributionConfig $Config -OriginId novamind-api-vpc -VpcOriginId vo_test `
        -InternalAlbDnsName 'internal.example.test' -CachePolicyId disabled-policy -RequestPolicyId auth-policy
    Assert-Equal $Config.Origins.Quantity 2 'Repeated hardening must not duplicate the API origin.'
    Assert-Equal $Config.CacheBehaviors.Quantity ($ExpectedExistingBehaviorCount + 1) 'Repeated hardening must not duplicate the API behavior.'
    $api = @($Config.CacheBehaviors.Items | Where-Object PathPattern -eq '/api/*')[0]
    Assert-Equal $api.TargetOriginId novamind-api-vpc 'API behavior must retain the VPC origin.'
    Assert-Equal $api.CachePolicyId disabled-policy 'API behavior must retain disabled caching.'
    Assert-Equal $api.OriginRequestPolicyId auth-policy 'API behavior must retain the auth request policy.'
}

Assert-ApiRoute -Config (New-TestDistributionConfig -ExistingPathPatterns @()) -ExpectedExistingBehaviorCount 0
Assert-ApiRoute -Config (New-TestDistributionConfig -ExistingPathPatterns @('/assets/*')) -ExpectedExistingBehaviorCount 1
Assert-ApiRoute -Config (New-TestDistributionConfig -ExistingPathPatterns @('/assets/*', '/docs/*')) -ExpectedExistingBehaviorCount 2
Write-Output 'CLOUDFRONT_COLLECTION_TESTS_PASS'
