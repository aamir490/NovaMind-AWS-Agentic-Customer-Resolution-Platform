<#
.SYNOPSIS
  Builds NovaMind's static frontend with public Cognito settings and publishes it to an existing private S3 origin.
.DESCRIPTION
  Default mode performs no Node, AWS, or network action. -Apply builds locally,
  uploads versioned assets without deleting unrelated S3 objects, updates only
  index.html with no-cache, and invalidates the HTML entrypoints on an existing
  CloudFront distribution. VITE values are public identifiers, never secrets.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$')] [string]$BucketName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Z0-9]{1,64}$')] [string]$DistributionId,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+(?:/[^?#]*)?$')] [string]$CognitoDomain,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')] [string]$CognitoClientId,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+(?:/[^?#]*)?$')] [string]$CallbackUri,
    [Parameter(Mandatory)] [ValidatePattern('^https://[^/?#]+(?:/[^?#]*)?$')] [string]$LogoutUri,
    [string]$ProjectRoot = (Join-Path $PSScriptRoot '..'),
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function Test-CanonicalHttpsUri {
    param([string]$Value, [string]$Name)
    try { $uri = [Uri]$Value } catch { throw "Invalid $Name." }
    if (-not $uri.IsAbsoluteUri -or $uri.Scheme -ne 'https' -or -not $uri.Host -or $uri.UserInfo -or
        $uri.Query -or $uri.Fragment -or $uri.Port -ne 443) {
        throw "Invalid $Name."
    }
    return $uri
}

$cognitoDomainUri = Test-CanonicalHttpsUri $CognitoDomain 'Cognito Hosted UI domain'
if ($cognitoDomainUri.AbsolutePath -ne '/') { throw 'Cognito Hosted UI domain must not contain a path.' }
Test-CanonicalHttpsUri $CallbackUri 'callback URI' | Out-Null
Test-CanonicalHttpsUri $LogoutUri 'logout URI' | Out-Null
if ($CallbackUri -ne $LogoutUri) { throw 'Use one canonical CloudFront HTTPS URI for the SPA callback and logout.' }
$frontendPath = Join-Path $ProjectRoot 'frontend'
if (-not (Test-Path -LiteralPath (Join-Path $frontendPath 'package.json') -PathType Leaf)) {
    throw 'ProjectRoot must contain frontend/package.json.'
}
if (-not $Apply) {
    Write-Output "Plan only: would build the frontend and publish it to existing S3 bucket $BucketName, then invalidate CloudFront $DistributionId."
    Write-Output 'No Node, AWS, S3, CloudFront, Docker, or HTTP operation was run.'
    return
}
foreach ($command in 'aws', 'npm') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "$command is required for -Apply." }
}

$environmentNames = @('VITE_COGNITO_DOMAIN', 'VITE_COGNITO_CLIENT_ID', 'VITE_COGNITO_REDIRECT_URI', 'VITE_COGNITO_LOGOUT_URI')
$oldEnvironment = @{}
foreach ($name in $environmentNames) { $oldEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    [Environment]::SetEnvironmentVariable('VITE_COGNITO_DOMAIN', $CognitoDomain, 'Process')
    [Environment]::SetEnvironmentVariable('VITE_COGNITO_CLIENT_ID', $CognitoClientId, 'Process')
    [Environment]::SetEnvironmentVariable('VITE_COGNITO_REDIRECT_URI', $CallbackUri, 'Process')
    [Environment]::SetEnvironmentVariable('VITE_COGNITO_LOGOUT_URI', $LogoutUri, 'Process')
    Push-Location $frontendPath
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    } finally {
        Pop-Location
    }
    $distPath = Join-Path $frontendPath 'dist'
    if (-not (Test-Path -LiteralPath (Join-Path $distPath 'index.html') -PathType Leaf)) {
        throw 'Frontend build did not produce dist/index.html.'
    }
    & aws --profile $ProfileName --region $Region s3 sync $distPath "s3://$BucketName" --exclude 'index.html' `
        --cache-control 'public,max-age=31536000,immutable' --no-cli-pager 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'S3 asset upload failed.' }
    & aws --profile $ProfileName --region $Region s3 cp (Join-Path $distPath 'index.html') "s3://$BucketName/index.html" `
        --cache-control 'no-cache, no-store, must-revalidate' --content-type 'text/html; charset=utf-8' --no-cli-pager 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'S3 index upload failed.' }
    $invalidation = (& aws --profile $ProfileName --region $Region cloudfront create-invalidation --distribution-id $DistributionId `
        --paths '/' '/index.html' --query 'Invalidation.Id' --output text --no-cli-pager 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $invalidation) { throw 'CloudFront invalidation request failed.' }
    Write-Output "Frontend published. CloudFront invalidation: $invalidation"
} finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $oldEnvironment[$name], 'Process') }
}
