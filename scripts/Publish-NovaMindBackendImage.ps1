<#
.SYNOPSIS
  Builds and pushes a versioned NovaMind backend image, then prints its immutable ECR digest.
.DESCRIPTION
  Default mode only validates local input. -Apply performs Docker/ECR operations.
  The image tag is a release label; downstream ECS deployment must use the digest
  emitted by this script, never a mutable tag.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9._/-]{1,240}$')]
    [string]$RepositoryUri,
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$')]
    [string]$ImageTag,
    [Parameter(Mandatory)]
    [ValidatePattern('^[A-Za-z0-9._-]{1,128}$')]
    [string]$SourceRevision,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$')]
    [string]$ExistingLocalImage,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')]
    [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()]
    [string]$ProfileName = 'novamind',
    [string]$ProjectRoot = (Join-Path $PSScriptRoot '..'),
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

$backendPath = Join-Path $ProjectRoot 'backend'
if (-not (Test-Path -LiteralPath (Join-Path $backendPath 'Dockerfile') -PathType Leaf)) {
    throw 'ProjectRoot must contain backend/Dockerfile.'
}
$repositoryName = $RepositoryUri.Substring($RepositoryUri.IndexOf('/') + 1)
$registry = $RepositoryUri.Substring(0, $RepositoryUri.IndexOf('/'))
$image = "${RepositoryUri}:$ImageTag"
if (-not $Apply) {
    $source = if ($ExistingLocalImage) { "retag existing local image $ExistingLocalImage" } else { "build from $backendPath" }
    Write-Output "Plan only: would $source and push $image to ECR."
    Write-Output 'No Docker or AWS command was run. The resulting digest would be required by the ECS deployment helper.'
    return
}
foreach ($command in 'aws', 'docker') {
    if (-not (Get-Command $command -ErrorAction SilentlyContinue)) { throw "$command is required for -Apply." }
}

# The ECR password is sent only on Docker stdin and is never captured or printed.
aws --profile $ProfileName --region $Region ecr get-login-password --no-cli-pager |
    docker login --username AWS --password-stdin $registry
if ($LASTEXITCODE -ne 0) { throw 'ECR login failed.' }
if ($ExistingLocalImage) {
    docker image inspect $ExistingLocalImage --format '{{.Id}}' *> $null
    if ($LASTEXITCODE -ne 0) { throw "Existing local image was not found: $ExistingLocalImage" }
    docker tag $ExistingLocalImage $image
    if ($LASTEXITCODE -ne 0) { throw 'Unable to retag the existing local backend image.' }
} else {
    throw 'Publishing a rebuilt image requires an explicitly reviewed CA-verified build workflow; provide -ExistingLocalImage for the already-verified image.'
}
docker push $image
if ($LASTEXITCODE -ne 0) { throw 'Backend image push failed.' }
$digest = (& aws --profile $ProfileName --region $Region ecr describe-images --repository-name $repositoryName `
    --image-ids "imageTag=$ImageTag" --query 'imageDetails[0].imageDigest' --output text --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $digest -notmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Unable to resolve the pushed image digest.'
}
Write-Output "Immutable backend image: $RepositoryUri@$digest"
