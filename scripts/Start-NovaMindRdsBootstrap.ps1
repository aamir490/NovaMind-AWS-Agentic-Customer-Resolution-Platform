<#
.SYNOPSIS
  Registers and starts one private, one-shot NovaMind RDS bootstrap ECS task.
.DESCRIPTION
  Default mode validates local input and does not call AWS. -Apply registers a
  task definition from the checked-in template, starts one Fargate task with
  assignPublicIp disabled, waits for completion, and reports only its exit
  status. It never fetches or displays a database or Secrets Manager value.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$MasterSecretArn,
    [Parameter(Mandatory)] [string]$ApplicationSecretArn,
    [Parameter(Mandatory)] [ValidatePattern('^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9._/-]{1,240}@sha256:[0-9a-f]{64}$')] [string]$BackendImageUri,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')] [string]$RdsEndpoint,
    [Parameter(Mandatory)] [string]$BootstrapTaskRoleArn,
    [Parameter(Mandatory)] [string]$ExecutionRoleArn,
    [Parameter(Mandatory)] [string]$ClusterName,
    [Parameter(Mandatory)] [Alias('PrivateSubnetId')] [ValidateCount(1,16)] [string[]]$SubnetId,
    [Parameter(Mandatory)] [string]$BootstrapSecurityGroupId,
    [Parameter(Mandatory)] [string]$CloudWatchLogGroup,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    # Explicitly opt in only after an operator has verified an IGW default
    # route, public IPv4 assignment, zero inbound rules, and RDS-SG access.
    [switch]$UsePublicBootstrapNetwork,
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

function Protect-AwsDiagnostic {
    param([AllowNull()] [string]$Text)
    $safe = [string]$Text
    $safe = $safe -replace '(?i)\b(password|secret|token|credential|access[_-]?key|session[_-]?token)\s*([=:])\s*[^\s,;]+', '$1$2[REDACTED]'
    $safe = $safe -replace '(?i)\b(authorization|x-amz-security-token|x-amz-credential)\s*([=:])\s*[^\s,;]+', '$1$2[REDACTED]'
    $safe = $safe -replace '[\r\n\t]+', ' '
    if ($safe.Length -gt 2000) { $safe = $safe.Substring(0, 2000) + ' [truncated]' }
    return $safe.Trim()
}

function Invoke-AwsCaptured {
    param([Parameter(Mandatory)] [string[]]$Arguments)

    $stdoutPath = New-TemporaryFile
    $stderrPath = New-TemporaryFile
    try {
        & aws @Arguments 1> $stdoutPath 2> $stderrPath
        $exitCode = $LASTEXITCODE
        $stdout = Get-Content -LiteralPath $stdoutPath -Raw -ErrorAction SilentlyContinue
        $stderr = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
        return [pscustomobject]@{
            ExitCode = $exitCode
            Stdout = [string]$stdout
            Stderr = [string]$stderr
        }
    } finally {
        Remove-Item -LiteralPath $stdoutPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

# RDS-managed master secrets use names such as ``rds!db-...``.  The exclamation
# mark is part of the ARN resource name, not a shell escape here.
$secretArn = '^arn:aws(?:-[a-z-]+)?:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@!\-]+$'
$roleArn = '^arn:aws(?:-[a-z-]+)?:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$'
if ($MasterSecretArn -notmatch $secretArn -or $ApplicationSecretArn -notmatch $secretArn) {
    throw 'MasterSecretArn and ApplicationSecretArn must be Secrets Manager ARNs.'
}
if ($BootstrapTaskRoleArn -notmatch $roleArn -or $ExecutionRoleArn -notmatch $roleArn) {
    throw 'BootstrapTaskRoleArn and ExecutionRoleArn must be IAM role ARNs.'
}
if ($ClusterName -notmatch '^[A-Za-z0-9_-]{1,255}$' -or
    $BootstrapSecurityGroupId -notmatch '^sg-[0-9a-f]+$' -or
    $CloudWatchLogGroup -notmatch '^[A-Za-z0-9_./#:-]{1,512}$' -or
    @($SubnetId | Where-Object { $_ -notmatch '^subnet-[0-9a-f]+$' }).Count -ne 0) {
    throw 'One or more ECS bootstrap identifiers are invalid.'
}

$templatePath = Join-Path $PSScriptRoot '..\infrastructure\novamind-rds-bootstrap-task-definition.template.json'
if (-not (Test-Path -LiteralPath $templatePath -PathType Leaf)) {
    throw 'Bootstrap task-definition template is missing.'
}
$template = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $templatePath), [System.Text.Encoding]::UTF8)
foreach ($marker in '__ECS_EXECUTION_ROLE_ARN__', '__BOOTSTRAP_TASK_ROLE_ARN__', '__BACKEND_IMAGE_URI__',
         '__AWS_REGION__', '__RDS_MASTER_SECRET_ARN__', '__APPLICATION_SECRET_ARN__', '__CLOUDWATCH_LOG_GROUP__',
         '__RDS_ENDPOINT__') {
    if (-not $template.Contains($marker)) { throw 'Bootstrap task-definition template is incomplete.' }
}
if (-not $Apply) {
    $networkKind = if ($UsePublicBootstrapNetwork) { 'public-IP-enabled' } else { 'private' }
    Write-Output "Plan only: would register and run one $networkKind Fargate bootstrap task in $($SubnetId -join ', ')."
    if ($UsePublicBootstrapNetwork) {
        Write-Output 'Public-network guard: verify IGW routing, zero inbound rules, narrow egress, and RDS SG port 5432 from this task SG before -Apply.'
    }
    Write-Output 'No AWS API call, secret read, database access, or ECS task start was performed. Re-run with -Apply after IAM policy review.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }

$replacements = [ordered]@{
    '__ECS_EXECUTION_ROLE_ARN__' = $ExecutionRoleArn
    '__BOOTSTRAP_TASK_ROLE_ARN__' = $BootstrapTaskRoleArn
    '__BACKEND_IMAGE_URI__' = $BackendImageUri
    '__AWS_REGION__' = $Region
    '__RDS_MASTER_SECRET_ARN__' = $MasterSecretArn
    '__APPLICATION_SECRET_ARN__' = $ApplicationSecretArn
    '__CLOUDWATCH_LOG_GROUP__' = $CloudWatchLogGroup
    '__RDS_ENDPOINT__' = $RdsEndpoint
}
foreach ($entry in $replacements.GetEnumerator()) { $template = $template.Replace($entry.Key, $entry.Value) }
try { $null = $template | ConvertFrom-Json -ErrorAction Stop } catch { throw 'Bootstrap task-definition template became invalid JSON.' }

$temporaryDefinition = New-TemporaryFile
try {
    [System.IO.File]::WriteAllText($temporaryDefinition, $template, [System.Text.UTF8Encoding]::new($false))
    $register = Invoke-AwsCaptured -Arguments @(
        '--profile', $ProfileName, '--region', $Region, 'ecs', 'register-task-definition',
        '--cli-input-json', ("file://{0}" -f $temporaryDefinition), '--query', 'taskDefinition.taskDefinitionArn',
        '--output', 'text', '--no-cli-pager')
    $taskDefinitionArn = ([string]$register.Stdout).Trim()
    if ($register.ExitCode -ne 0 -or $taskDefinitionArn -notmatch '^arn:aws(?:-[a-z-]+)?:ecs:') {
        $detail = Protect-AwsDiagnostic ($register.Stderr + ' ' + $register.Stdout)
        if (-not $detail) { $detail = 'No AWS CLI diagnostic was returned.' }
        throw "ECS refused bootstrap task-definition registration (AWS CLI exit code $($register.ExitCode)): $detail"
    }
} finally {
    Remove-Item -LiteralPath $temporaryDefinition -Force -ErrorAction SilentlyContinue
}

$assignPublicIp = if ($UsePublicBootstrapNetwork) { 'ENABLED' } else { 'DISABLED' }
$network = 'awsvpcConfiguration={subnets=[' + ($SubnetId -join ',') +
    '],securityGroups=[' + $BootstrapSecurityGroupId + '],assignPublicIp=' + $assignPublicIp + '}'
$run = Invoke-AwsCaptured -Arguments @(
    '--profile', $ProfileName, '--region', $Region, 'ecs', 'run-task', '--cluster', $ClusterName,
    '--launch-type', 'FARGATE', '--task-definition', $taskDefinitionArn, '--count', '1',
    '--network-configuration', $network, '--output', 'json', '--no-cli-pager')
$runResponse = $null
if (([string]$run.Stdout).Trim()) {
    try { $runResponse = $run.Stdout | ConvertFrom-Json -ErrorAction Stop } catch { $runResponse = $null }
}
$taskArn = if ($null -ne $runResponse -and @($runResponse.tasks).Count -gt 0) { [string]$runResponse.tasks[0].taskArn } else { $null }
$failureDetails = @()
if ($null -ne $runResponse) {
    $failureDetails = @($runResponse.failures | ForEach-Object {
        $parts = @([string]$_.arn, [string]$_.reason, [string]$_.detail) | Where-Object { $_ -and $_ -ne 'null' }
        ($parts -join ': ')
    } | Where-Object { $_ })
}
if ($run.ExitCode -ne 0 -or $taskArn -notmatch '^arn:aws(?:-[a-z-]+)?:ecs:') {
    $detail = Protect-AwsDiagnostic (($failureDetails -join '; ') + ' ' + $run.Stderr)
    if (-not $detail) { $detail = Protect-AwsDiagnostic $run.Stdout }
    if (-not $detail) { $detail = 'No AWS CLI diagnostic or ECS failure was returned.' }
    throw "ECS refused bootstrap task start (AWS CLI exit code $($run.ExitCode)): $detail"
}
$wait = Invoke-AwsCaptured -Arguments @('--profile', $ProfileName, '--region', $Region, 'ecs', 'wait', 'tasks-stopped', '--cluster', $ClusterName, '--tasks', $taskArn, '--no-cli-pager')
if ($wait.ExitCode -ne 0) {
    $detail = Protect-AwsDiagnostic ($wait.Stderr + ' ' + $wait.Stdout)
    if (-not $detail) { $detail = 'No AWS CLI diagnostic was returned.' }
    throw "Timed out waiting for the bootstrap task (AWS CLI exit code $($wait.ExitCode)): $detail"
}
$describe = Invoke-AwsCaptured -Arguments @(
    '--profile', $ProfileName, '--region', $Region, 'ecs', 'describe-tasks', '--cluster', $ClusterName,
    '--tasks', $taskArn, '--query', 'tasks[0].{LastStatus:lastStatus,ExitCode:containers[0].exitCode}',
    '--output', 'json', '--no-cli-pager')
try { $result = $describe.Stdout | ConvertFrom-Json -ErrorAction Stop } catch {
    $detail = Protect-AwsDiagnostic ($describe.Stderr + ' ' + $describe.Stdout)
    throw "Unable to verify bootstrap task status: $detail"
}
if ($result.LastStatus -ne 'STOPPED' -or $result.ExitCode -ne 0) {
    throw 'Bootstrap task failed. Do not create another application secret; inspect the CloudWatch bootstrap log and rerun only after correcting the cause.'
}
Write-Output "Bootstrap task completed successfully: $taskArn"
