Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $workspace 'scripts/Start-NovaMindRdsBootstrap.ps1'
$source = Get-Content -LiteralPath $scriptPath -Raw

$tokens = $null
$parseErrors = $null
[System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$parseErrors) > $null
if ($parseErrors.Count -ne 0) { throw 'Bootstrap start script did not parse.' }
foreach ($marker in @(
    'function Protect-AwsDiagnostic',
    'function Invoke-AwsCaptured',
    'failures',
    "'--output', 'json'",
    'AWS CLI exit code',
    'REDACTED',
    '([string]$register.Stdout).Trim()',
    '([string]$run.Stdout).Trim()',
    'subnets=[',
    '],securityGroups=['
)) {
    if ($source -notmatch [regex]::Escape($marker)) { throw "Missing diagnostic contract: $marker" }
}
if ($source -match '2>\s*\$null') { throw 'AWS diagnostics must not be discarded.' }
if ($source -match 'System\.Text\.Json') { throw 'Bootstrap workflow must remain Windows PowerShell compatible.' }

$plan = & $scriptPath `
    -MasterSecretArn 'arn:aws:secretsmanager:us-east-1:357001292388:secret:rds!db-example' `
    -ApplicationSecretArn 'arn:aws:secretsmanager:us-east-1:357001292388:secret:novamind-crp-postgres-app-dev-example' `
    -BackendImageUri '357001292388.dkr.ecr.us-east-1.amazonaws.com/novamind-crp-backend-dev@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' `
    -RdsEndpoint 'db.example.rds.amazonaws.com' `
    -BootstrapTaskRoleArn 'arn:aws:iam::357001292388:role/novamind-rds-bootstrap-task' `
    -ExecutionRoleArn 'arn:aws:iam::357001292388:role/novamind-crp-bootstrap-execution-dev' `
    -ClusterName 'novamind-crp-ecs-cluster-dev' `
    -SubnetId @('subnet-0abc1234', 'subnet-0def5678') `
    -BootstrapSecurityGroupId 'sg-0abc1234' `
    -CloudWatchLogGroup '/ecs/novamind-rds-bootstrap' `
    -ProfileName 'novamind' -Region 'us-east-1'
$planText = $plan -join "`n"
if ($planText -notmatch 'one private Fargate bootstrap task') { throw 'Plan did not preserve private bootstrap mode.' }
if ($planText -notmatch 'No AWS API call, secret read, database access, or ECS task start was performed') {
    throw 'Plan-only validation did not confirm that AWS work was skipped.'
}

Write-Output 'START_RDS_BOOTSTRAP_DIAGNOSTICS_TEST_PASS'
