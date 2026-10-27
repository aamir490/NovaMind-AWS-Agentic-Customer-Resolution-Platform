<#
.SYNOPSIS
  NovaMind AI - one-shot, READ-ONLY AWS deployment preflight.
.DESCRIPTION
  Inspects local Git/source/Docker and AWS resource metadata for deployment planning.
  Does NOT deploy, create, update, delete, invoke Bedrock models, or change Docker.
  Generates a sanitized JSON summary under %TEMP% only (not inside the repo).
.EXAMPLE
  & "$env:USERPROFILE\Downloads\NovaMind-AWS-Preflight.ps1" -ProjectRoot 'E:\codex-projetc\NovaMind-local-smoke-test' -ProfileName novamind -Region us-east-1
.EXAMPLE
  & ".\NovaMind-AWS-Preflight.ps1" -ProjectRoot 'E:\codex-projetc\NovaMind-local-smoke-test' -ExpectedAccountId '123456789012'
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Get-Location).Path,
    [string]$ProfileName = 'novamind',
    [string]$Region = 'us-east-1',
    [ValidatePattern('^(|[0-9]{12})$')]
    [string]$ExpectedAccountId = '',
    [switch]$SkipAWS
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

$script:checks = New-Object 'System.Collections.Generic.List[object]'
$script:awsReady = $false

function Add-Check {
    param([string]$Area, [string]$Name, [string]$Status, [string]$Note)
    $item = [pscustomobject]@{ Area=$Area; Check=$Name; Status=$Status; Detail=$Note }
    $script:checks.Add($item)
    $color = switch ($Status) {
        'OK' {'Green'}
        'BLOCK' {'Red'}
        'REVIEW' {'Yellow'}
        'SKIP' {'DarkGray'}
        default {'Cyan'}
    }
    Write-Host ('[{0,-6}] {1} / {2}: {3}' -f $Status,$Area,$Name,$Note) -ForegroundColor $color
}

function Has-Command([string]$Name) {
    return ($null -ne (Get-Command $Name -ErrorAction SilentlyContinue))
}

function Invoke-Text {
    param([string]$Exe, [string[]]$ArgumentList)
    try {
        # Do not print stdout/stderr from failed commands (could include sensitive context).
        $lines = & $Exe @ArgumentList 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return (($lines | Out-String).Trim())
    } catch { return $null }
}

function Invoke-AwsJson {
    param([string[]]$ServiceArgs)
    $full = @('--profile', $ProfileName, '--region', $Region) + $ServiceArgs + @('--output','json','--no-cli-pager','--cli-connect-timeout','5','--cli-read-timeout','15')
    $result = Invoke-Text -Exe 'aws' -ArgumentList $full
    if ([string]::IsNullOrWhiteSpace($result)) {
        return [pscustomobject]@{ OK=$false; Data=$null }
    }
    try {
        $data = ConvertFrom-Json -InputObject $result -ErrorAction Stop
        return [pscustomobject]@{ OK=$true; Data=$data }
    } catch {
        return [pscustomobject]@{ OK=$false; Data=$null }
    }
}

function Get-CollectionCount {
    param($Object, [string]$PropertyPath)
    $value = $Object
    foreach ($part in $PropertyPath.Split('.')) {
        if ($null -eq $value) { return 0 }
        $property = $value.PSObject.Properties[$part]
        if ($null -eq $property) { return 0 }
        $value = $property.Value
    }
    if ($null -eq $value) { return 0 }
    return @($value).Count
}

function Audit-AwsCollection {
    param([string]$Name, [string[]]$CommandArgs, [string]$PropertyPath, [string]$Meaning)
    $response = Invoke-AwsJson -ServiceArgs $CommandArgs
    if (-not $response.OK) {
        Add-Check 'AWS INVENTORY' $Name 'REVIEW' 'Read request unavailable (permissions, network, or API support); verify manually.'
        return
    }
    $count = Get-CollectionCount -Object $response.Data -PropertyPath $PropertyPath
    Add-Check 'AWS INVENTORY' $Name 'INFO' ("Found {0}. {1}" -f $count, $Meaning)
}

Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' NovaMind AI / AWS DEPLOYMENT PREFLIGHT (NO DEPLOYMENT ACTIONS)' -ForegroundColor Cyan
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ("Project: {0}" -f $ProjectRoot)
Write-Host ("AWS profile: {0} | Region: {1}" -f $ProfileName,$Region)
Write-Host 'This checks metadata only. NO aws create/update/delete; NO Bedrock invoke.'
Write-Host ''

# ----------------- Local source and Git -----------------
if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    Add-Check 'LOCAL' 'Project directory' 'BLOCK' 'ProjectRoot folder does not exist.'
} else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
    Add-Check 'LOCAL' 'Project directory' 'OK' 'Project folder exists.'
}

$hasGit = Has-Command 'git'
$hasDocker = Has-Command 'docker'
$hasAWS = Has-Command 'aws'
Add-Check 'TOOLS' 'Git' $(if($hasGit){'OK'}else{'BLOCK'}) $(if($hasGit){'Installed'}else{'Missing from PATH'})
Add-Check 'TOOLS' 'Docker' $(if($hasDocker){'OK'}else{'REVIEW'}) $(if($hasDocker){'Installed'}else{'Missing from PATH'})
Add-Check 'TOOLS' 'AWS CLI' $(if($hasAWS){'OK'}else{'BLOCK'}) $(if($hasAWS){'Installed'}else{'Missing from PATH'})

if ((Test-Path -LiteralPath $ProjectRoot -PathType Container) -and $hasGit) {
    $root = Invoke-Text -Exe 'git' -ArgumentList @('-C',$ProjectRoot,'rev-parse','--show-toplevel')
    if ($null -eq $root) {
        Add-Check 'GIT' 'Repository' 'BLOCK' 'Not recognized as a Git working tree.'
    } else {
        Add-Check 'GIT' 'Repository' 'OK' 'Git working tree detected.'
        $branch = Invoke-Text 'git' @('-C',$ProjectRoot,'branch','--show-current')
        $commit = Invoke-Text 'git' @('-C',$ProjectRoot,'rev-parse','--short','HEAD')
        Add-Check 'GIT' 'Current checkout' 'INFO' ("Branch={0}; commit={1}" -f $branch,$commit)
        $dirty = Invoke-Text 'git' @('-C',$ProjectRoot,'status','--porcelain=v1','--untracked-files=all')
        if ([string]::IsNullOrWhiteSpace($dirty)) {
            Add-Check 'GIT' 'Working tree' 'OK' 'No uncommitted or untracked files.'
        } else {
            $changed = @($dirty -split "`n" | Where-Object { $_.Trim() }).Count
            Add-Check 'GIT' 'Working tree' 'REVIEW' ("{0} file entries differ locally. Do not overwrite them during deployment." -f $changed)
        }
        $localMain = Invoke-Text 'git' @('-C',$ProjectRoot,'rev-parse','main')
        $remoteMain = Invoke-Text 'git' @('-C',$ProjectRoot,'rev-parse','origin/main')
        if ($localMain -and $remoteMain -and $localMain -eq $remoteMain) {
            Add-Check 'GIT' 'Local main vs origin/main' 'OK' 'Matching locally cached commit hashes (not a fresh remote fetch).'
        } else {
            Add-Check 'GIT' 'Local main vs origin/main' 'REVIEW' 'Cached branches differ or could not be read.'
        }
        $paths = Invoke-Text 'git' @('-C',$ProjectRoot,'ls-files')
        if ($null -ne $paths) {
            $suspicious = @($paths -split "`n" | Where-Object {
                $p = $_.Trim().Replace('\','/')
                (($p -match '(^|/)\.env($|\.)' -and $p -notmatch '\.example$') -or
                 $p -match '(^|/)\.aws/' -or
                 $p -match '(?i)\.(sqlite3?|db)(-(wal|shm|journal))?$' -or
                 $p -match '(?i)\.(pem|key)$')
            })
            if ($suspicious.Count -gt 0) {
                Add-Check 'GIT' 'Sensitive tracked filenames' 'BLOCK' ("{0} tracked filename(s) require manual review; no contents displayed." -f $suspicious.Count)
            } else {
                Add-Check 'GIT' 'Sensitive tracked filenames' 'OK' 'No obvious tracked secret/database filenames (pattern check only).'
            }
        }
    }
}

$required = @(
    'backend/Dockerfile', 'backend/app/main.py', 'backend/app/graph_agent.py',
    'backend/app/dispatcher.py', 'backend/app/security.py',
    'backend/app/persistence.py', 'backend/app/langgraph_checkpoints.py',
    'backend/knowledge/support.json', 'backend/requirements.txt',
    'frontend/package.json', 'frontend/src/api.js', 'compose.yaml',
    '.github/workflows/ci.yml'
)
if (Test-Path -LiteralPath $ProjectRoot -PathType Container) {
    $missing = @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $ProjectRoot $_) -PathType Leaf) })
    if ($missing.Count -eq 0) {
        Add-Check 'SOURCE' 'Project essentials' 'OK' 'All 13 expected key source/config files exist.'
    } else {
        Add-Check 'SOURCE' 'Project essentials' 'BLOCK' ('Missing: ' + ($missing -join ', '))
    }
    $workflowPath = Join-Path $ProjectRoot '.github/workflows/ci.yml'
    if (Test-Path -LiteralPath $workflowPath -PathType Leaf) {
        $ci = [IO.File]::ReadAllText($workflowPath)
        if ($ci -match 'pull_request' -and $ci -match 'unittest' -and $ci -match 'npm') {
            Add-Check 'CI' 'PR offline workflow' 'OK' 'Workflow contains PR trigger, Python tests, and Node testing/build signals.'
        } else {
            Add-Check 'CI' 'PR offline workflow' 'REVIEW' 'CI workflow exists; manually verify event triggers and test commands.'
        }
    }
    $persistenceFile = Join-Path $ProjectRoot 'backend/app/persistence.py'
    $requirementsFile = Join-Path $ProjectRoot 'backend/requirements.txt'
    if ((Test-Path -LiteralPath $persistenceFile) -and (Test-Path -LiteralPath $requirementsFile)) {
        $persistSource = [IO.File]::ReadAllText($persistenceFile)
        $requirements = [IO.File]::ReadAllText($requirementsFile)
        if ($persistSource -match '(?i)sqlite3|sqlite' -and $requirements -notmatch '(?im)^(psycopg|asyncpg|psycopg2)(\[|[=<>])') {
            Add-Check 'MIGRATION' 'Database' 'REVIEW' 'SQLite references found; no common PostgreSQL driver in pinned requirements. Design and test RDS migration before ECS multi-task deployment.'
        } else {
            Add-Check 'MIGRATION' 'Database' 'REVIEW' 'Review actual database adapter, migrations, checkpoint transactions, and RDS readiness in code.'
        }
    }
    $backendApp = Join-Path $ProjectRoot 'backend/app'
    if (Test-Path -LiteralPath $backendApp) {
        $authCandidates = @(Get-ChildItem -LiteralPath $backendApp -Filter '*.py' -File -ErrorAction SilentlyContinue |
            Select-String -Pattern '(?i)cognito|jwks|jwt' -List -ErrorAction SilentlyContinue)
        if ($authCandidates.Count -gt 0) {
            Add-Check 'MIGRATION' 'Cognito' 'REVIEW' 'Possible auth-related implementation found; verify JWKS/JWT validation, role mapping, and customer ownership.'
        } else {
            Add-Check 'MIGRATION' 'Cognito' 'REVIEW' 'No clear Cognito/JWT references found in backend app Python files; integration may still be required.'
        }
    }
    Add-Check 'MIGRATION' 'ECS dispatcher' 'REVIEW' 'Verify durable worker startup, lease recovery, and single-worker safety before scaling Fargate tasks.'
    Add-Check 'MIGRATION' 'Frontend HTTPS' 'REVIEW' 'Verify API URL, CORS, CloudFront origin access, cookie/JWT behavior and ACM certificate/domain plan.'
}

if ($hasDocker) {
    $dockerVersion = Invoke-Text 'docker' @('info','--format','{{.ServerVersion}}')
    if ($dockerVersion) {
        Add-Check 'DOCKER' 'Daemon' 'OK' ("Docker engine responds, version {0}." -f $dockerVersion)
    } else {
        Add-Check 'DOCKER' 'Daemon' 'REVIEW' 'Docker command found, but daemon unavailable.'
    }
    # No restart, image build, container creation, or docker compose up.
}

# ----------------- AWS account and resource inventory -----------------
if ($SkipAWS) {
    Add-Check 'AWS' 'Account lookup' 'SKIP' 'Skipped by -SkipAWS.'
} elseif (-not $hasAWS) {
    Add-Check 'AWS' 'Account lookup' 'BLOCK' 'AWS CLI unavailable.'
} else {
    $who = Invoke-AwsJson -ServiceArgs @('sts','get-caller-identity')
    if (-not $who.OK -or -not $who.Data.Account) {
        Add-Check 'AWS' 'Account lookup' 'BLOCK' 'STS identity failed: check profile, credentials, MFA/SSO, or connectivity.'
    } else {
        $actualAccount = [string]$who.Data.Account
        $masked = ('********{0}' -f $actualAccount.Substring([Math]::Max(0, $actualAccount.Length - 4)))
        $principalType = if ([string]$who.Data.Arn -match ':assumed-role/') { 'Assumed role' } elseif ([string]$who.Data.Arn -match ':user/') { 'IAM user' } else { 'Other principal' }
        Add-Check 'AWS' 'Authenticated identity' 'OK' ("Account={0}; type={1}; region={2}" -f $masked,$principalType,$Region)
        if ($ExpectedAccountId) {
            if ($actualAccount -ne $ExpectedAccountId) {
                Add-Check 'AWS' 'Expected account guard' 'BLOCK' 'ACCOUNT MISMATCH: STOP. Inventory and deployment are unsafe until corrected.'
            } else {
                Add-Check 'AWS' 'Expected account guard' 'OK' 'Authenticated account matches ExpectedAccountId.'
                $script:awsReady = $true
            }
        } else {
            Add-Check 'AWS' 'Expected account guard' 'REVIEW' 'ExpectedAccountId not supplied; verify the masked account and profile before any deployment.'
            $script:awsReady = $true
        }
    }
}

if ($script:awsReady) {
    Write-Host ''
    Write-Host '--- AWS metadata inventory (existing resources only; zero is normal) ---' -ForegroundColor Cyan
    Audit-AwsCollection 'VPCs' @('ec2','describe-vpcs') 'Vpcs' 'Confirm VPC/subnet design before deployment.'
    Audit-AwsCollection 'Subnets' @('ec2','describe-subnets') 'Subnets' 'Subnet counts do not prove public/private routing.'
    Audit-AwsCollection 'Route tables' @('ec2','describe-route-tables') 'RouteTables' 'Review actual IGW/NAT routing separately.'
    Audit-AwsCollection 'Internet gateways' @('ec2','describe-internet-gateways') 'InternetGateways' 'Required for selected public ingress patterns.'
    Audit-AwsCollection 'NAT gateways' @('ec2','describe-nat-gateways') 'NatGateways' 'A provisioned NAT gateway may incur ongoing hourly charges.'
    Audit-AwsCollection 'VPC endpoints' @('ec2','describe-vpc-endpoints') 'VpcEndpoints' 'May reduce network egress requirements for private ECS.'
    Audit-AwsCollection 'ECS clusters' @('ecs','list-clusters') 'clusterArns' 'Existing clusters are not required for preflight.'
    Audit-AwsCollection 'ECR repositories' @('ecr','describe-repositories') 'repositories' 'Docker images will be pushed here during deployment.'
    Audit-AwsCollection 'Load balancers' @('elbv2','describe-load-balancers') 'LoadBalancers' 'ALB creates continuing hourly and capacity charges.'
    Audit-AwsCollection 'S3 buckets' @('s3api','list-buckets') 'Buckets' 'Global account-wide count, not necessarily target-region resources.'
    Audit-AwsCollection 'CloudFront distributions' @('cloudfront','list-distributions') 'DistributionList.Items' 'Global account-wide count.'
    Audit-AwsCollection 'Cognito user pools' @('cognito-idp','list-user-pools','--max-results','60') 'UserPools' 'Up to 60 returned; Cognito integration still needs code changes.'
    Audit-AwsCollection 'RDS DB instances' @('rds','describe-db-instances') 'DBInstances' 'PostgreSQL persistence/migrations must be implemented and verified.'
    Audit-AwsCollection 'RDS DB clusters' @('rds','describe-db-clusters') 'DBClusters' 'Existing clusters may incur continuing charges.'
    Audit-AwsCollection 'ACM issued certificates' @('acm','list-certificates','--certificate-statuses','ISSUED') 'CertificateSummaryList' 'ALB certificates must be in the ALB region; CloudFront certs in us-east-1.'
    Audit-AwsCollection 'Secrets Manager entries' @('secretsmanager','list-secrets','--max-results','100') 'SecretList' 'Metadata only; no GetSecretValue calls.'
    Audit-AwsCollection 'ECS log groups' @('logs','describe-log-groups','--log-group-name-prefix','/ecs/') 'logGroups' 'CloudWatch log group count under /ecs/ prefix.'
    Audit-AwsCollection 'IAM OIDC providers' @('iam','list-open-id-connect-providers') 'OpenIDConnectProviderList' 'Verify GitHub-specific OIDC provider and restricted deploy role.'
    $profiles = Invoke-AwsJson -ServiceArgs @('bedrock','list-inference-profiles')
    if ($profiles.OK) {
        $profileProp = $profiles.Data.PSObject.Properties['inferenceProfileSummaries']
        $summaries = if ($null -ne $profileProp -and $null -ne $profileProp.Value) { @($profileProp.Value) } else { @() }
        $novaProfiles = @($summaries | Where-Object { [string]$_.inferenceProfileId -match 'nova-lite' })
        Add-Check 'AWS INVENTORY' 'Bedrock inference profiles' 'INFO' ("{0} accessible profiles; {1} with nova-lite in ID. Listing does NOT verify invoke permissions or model access." -f $summaries.Count,$novaProfiles.Count)
    } else {
        Add-Check 'AWS INVENTORY' 'Bedrock inference profiles' 'REVIEW' 'Unable to list (permissions, region, or API support). No model invoked.'
    }
    Add-Check 'AWS' 'Cost guard' 'REVIEW' 'Review AWS Budgets and current bill before creating RDS, ALB, Fargate, NAT, or CloudFront resources.'
}

# ----------------- Summary and sanitized local report -----------------
Write-Host ''
Write-Host '===============================================================' -ForegroundColor Cyan
Write-Host ' FINAL PREFLIGHT SUMMARY (deployment NOT started)' -ForegroundColor Cyan
Write-Host '===============================================================' -ForegroundColor Cyan
$blocked = @($script:checks | Where-Object { $_.Status -eq 'BLOCK' }).Count
$reviews = @($script:checks | Where-Object { $_.Status -eq 'REVIEW' }).Count
$passed  = @($script:checks | Where-Object { $_.Status -eq 'OK' }).Count
Write-Host ("OK={0} | REVIEW={1} | BLOCK={2}" -f $passed,$reviews,$blocked)
Write-Host 'RESOURCE COUNTS ARE INFORMATIONAL, NOT DEPLOYMENT APPROVAL.' -ForegroundColor Yellow
Write-Host 'MANDATORY MANUAL AUDIT: Cognito/JWT, SQLite->PostgreSQL, ALB/VPC/HTTPS, ECS task IAM, worker durability and CI/CD OIDC.' -ForegroundColor Yellow
Write-Host 'No resources created, deleted, changed, or charged model invocations initiated.'

try {
    $directory = Join-Path $env:TEMP 'NovaMindAI-Readiness'
    if (-not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    $reportPath = Join-Path $directory ('preflight-{0}.json' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $report = [pscustomobject]@{
        project = 'NovaMind AI Customer Resolution Platform'
        timestamp_local = (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssK')
        aws_profile = $ProfileName
        aws_region = $Region
        account_id = 'MASKED; verify locally before deployment'
        operations = 'READ ONLY: Git, local files, Docker metadata, AWS describe/list/get identity'
        summary = [pscustomobject]@{ ok=$passed; review=$reviews; block=$blocked }
        checks = @($script:checks.ToArray())
    }
    $report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $reportPath -Encoding UTF8
    Write-Host ("Sanitized report: {0}" -f $reportPath) -ForegroundColor Cyan
} catch {
    Write-Host 'Report file could not be saved; all checks were still printed.' -ForegroundColor Yellow
}
Write-Host 'Share only the FINAL SUMMARY and BLOCK/REVIEW lines, after checking for sensitive context.' -ForegroundColor Cyan
