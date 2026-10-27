<#
.SYNOPSIS
  Registers an immutable NovaMind API revision and deploys it to an existing ECS service.
.DESCRIPTION
  Default mode validates only local release input. -Apply reads ECS task/service
  metadata, removes the local-auth/SQLite configuration from the named API
  container, injects the least-privilege RDS application secret reference and
  Cognito configuration, registers a new task-definition revision, and updates
  the existing service. It never calls GetSecretValue and never accepts the RDS
  master-secret ARN as input.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,255}$')] [string]$ClusterName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,255}$')] [string]$ServiceName,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,255}$')] [string]$ContainerName,
    [Parameter(Mandatory)] [ValidatePattern('^[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9._/-]{1,240}@sha256:[0-9a-f]{64}$')] [string]$ImageUri,
    [Parameter(Mandatory)] [ValidatePattern('^arn:aws(?:-[a-z-]+)?:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@!\-]+$')] [string]$ApplicationSecretArn,
    [Parameter(Mandatory)] [ValidatePattern('^https://cognito-idp\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?/[A-Za-z0-9_-]{1,128}$')] [string]$CognitoIssuer,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_-]{1,128}$')] [string]$CognitoClientId,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_+=,.@-]{1,128}$')] [string]$CognitoCustomerGroup,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_+=,.@-]{1,128}$')] [string]$CognitoReviewerGroup,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9_+=,.@-]{1,128}$')] [string]$CognitoAdminGroup,
    [Parameter(Mandatory)] [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$')] [string]$BedrockModelId,
    [Parameter(Mandatory)] [Alias('PrivateEgressMode')] [ValidateSet('PublicTaskEgress', 'ExistingNat', 'ApprovedEgressProxy')] [string]$EgressMode,
    [ValidateLength(2, 65536)] [string]$CognitoCustomerBindingsJson = '[]',
    [switch]$EnableAutomaticDispatcher,
    [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')] [string]$DispatcherUserId,
    [ValidateSet('REVIEWER', 'ADMIN')] [string]$DispatcherRole,
    [switch]$CreateMissingService,
    [ValidatePattern('^[A-Za-z0-9_./:-]{1,255}$')] [string]$BaseTaskDefinition,
    [ValidatePattern('^arn:aws(?:-[a-z-]+)?:elasticloadbalancing:[a-z0-9-]+:[0-9]{12}:targetgroup/[A-Za-z0-9-]+/[0-9a-f]+$')] [string]$TargetGroupArn,
    [ValidateCount(1, 16)] [string[]]$ApplicationPublicSubnetIds,
    [ValidatePattern('^sg-[0-9a-f]+$')] [string]$ApiSecurityGroupId,
    [switch]$AllowNoDeploymentCircuitBreaker,
    [switch]$ForwardOnlyMigrationCutover,
    [ValidatePattern('^[A-Za-z0-9_./:-]{1,255}$')] [string]$ExpectedCurrentTaskDefinition,
    [ValidateScript({ if (-not (Test-Path -LiteralPath $_ -PathType Leaf)) { throw 'Migration preflight evidence file was not found.' }; $true })]
    [string]$MigrationPreflightEvidencePath,
    [ValidatePattern('^https://[^\s]{1,2048}$')] [string]$HealthUrl,
    [ValidatePattern('^[a-z]{2}(?:-[a-z0-9]+){1,3}-[0-9]{1,2}$')] [string]$Region = 'us-east-1',
    [ValidateNotNullOrEmpty()] [string]$ProfileName = 'novamind',
    [switch]$Apply
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$env:AWS_PAGER = ''
$env:AWS_CLI_AUTO_PROMPT = 'off'
$env:AWS_EC2_METADATA_DISABLED = 'true'

if ($CognitoCustomerGroup -in @($CognitoReviewerGroup, $CognitoAdminGroup) -or $CognitoReviewerGroup -eq $CognitoAdminGroup) {
    throw 'Cognito group names must be distinct.'
}
try {
    # ConvertFrom-Json unwraps a one-element JSON array into PSCustomObject.
    # Validate the raw JSON shape first, then retain it in an explicit PowerShell
    # array so a single trusted binding remains an array when rendered for ECS.
    $document = [System.Text.Json.JsonDocument]::Parse($CognitoCustomerBindingsJson)
    try {
        if ($document.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Array) {
            throw 'invalid bindings'
        }
    } finally {
        $document.Dispose()
    }
    $bindings = @($CognitoCustomerBindingsJson | ConvertFrom-Json -ErrorAction Stop)
    if ($bindings.Count -gt 1000) {
        throw 'invalid bindings'
    }
    $CognitoCustomerBindingsJson = if ($bindings.Count -eq 0) { '[]' } else { ConvertTo-Json -InputObject $bindings -Compress -Depth 20 }
} catch {
    throw 'CognitoCustomerBindingsJson must be a bounded JSON array.'
}
if ($EnableAutomaticDispatcher) {
    if (-not $DispatcherUserId -or -not $DispatcherRole) {
        throw 'Automatic dispatcher requires a canonical service UUID and a REVIEWER or ADMIN role.'
    }
} elseif ($DispatcherUserId -or $DispatcherRole) {
    throw 'Dispatcher identity must not be supplied unless automatic dispatch is explicitly enabled.'
}
if ($CreateMissingService) {
    if (-not $BaseTaskDefinition -or -not $TargetGroupArn -or -not $ApplicationPublicSubnetIds -or -not $ApiSecurityGroupId) {
        throw 'Creating a missing service requires BaseTaskDefinition, TargetGroupArn, ApplicationPublicSubnetIds, and ApiSecurityGroupId.'
    }
} elseif ($BaseTaskDefinition -or $TargetGroupArn -or $ApplicationPublicSubnetIds -or $ApiSecurityGroupId) {
    throw 'Service-creation inputs require -CreateMissingService.'
}
if ($ForwardOnlyMigrationCutover) {
    # Keep this deployment gate aligned with the highest checked-in
    # backend.app.postgres_persistence.POSTGRES_MIGRATIONS version.
    $requiredPostgresSchemaVersion = 13
    if ($CreateMissingService) {
        throw 'Forward-only migration cutover requires the existing ECS service; service creation is not permitted.'
    }
    if ($EnableAutomaticDispatcher) {
        throw 'Forward-only migration cutover requires the automatic dispatcher to remain disabled.'
    }
    if (-not $ExpectedCurrentTaskDefinition) {
        throw 'Forward-only migration cutover requires ExpectedCurrentTaskDefinition.'
    }
    if (-not $MigrationPreflightEvidencePath) {
        throw 'Forward-only migration cutover requires MigrationPreflightEvidencePath.'
    }
    if (-not $HealthUrl) {
        throw 'Forward-only migration cutover requires HealthUrl.'
    }
    try {
        $migrationEvidence = Get-Content -LiteralPath $MigrationPreflightEvidencePath -Raw | ConvertFrom-Json -ErrorAction Stop
        if ([string]$migrationEvidence.Database -ne 'novamindcrp' -or
            [string]$migrationEvidence.Schema -ne 'novamind' -or
            [string]$migrationEvidence.ApplicationRole -ne 'novamind_app' -or
            [string]$migrationEvidence.SchemaOwner -ne 'novamind_app' -or
            $migrationEvidence.DdlPermissions -ne $true -or
            [int]$migrationEvidence.TargetSchemaVersion -ne $requiredPostgresSchemaVersion -or
            [int]$migrationEvidence.CurrentSchemaVersion -ne $requiredPostgresSchemaVersion) {
            throw 'migration evidence values are not approved'
        }
        $verifiedAtValue = $migrationEvidence.VerifiedAtUtc
        if ($verifiedAtValue -is [DateTimeOffset]) {
            $verifiedAt = [DateTimeOffset]$verifiedAtValue
        } elseif ($verifiedAtValue -is [DateTime]) {
            $verifiedAt = [DateTimeOffset]$verifiedAtValue
        } else {
            $verifiedAt = [DateTimeOffset]::Parse(
                [string]$verifiedAtValue,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::RoundtripKind)
        }
        if ($verifiedAt -gt [DateTimeOffset]::UtcNow.AddMinutes(5) -or
            $verifiedAt -lt [DateTimeOffset]::UtcNow.AddHours(-24)) {
            throw 'migration evidence is stale'
        }
    } catch {
        throw 'Migration preflight evidence must prove novamindcrp/novamind ownership, DDL permissions, and current schema version 13 targeting version 13.'
    }
}

function Set-JsonProperty {
    param([object]$Object, [string]$Name, $Value)
    $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
}

function Assert-DeploymentCircuitBreakerSafety {
    param(
        [Parameter(Mandatory)] [object]$CircuitBreaker,
        [Parameter(Mandatory)] [bool]$IsForwardOnlyCutover,
        [Parameter(Mandatory)] [bool]$AllowMissingRollback
    )
    if ($IsForwardOnlyCutover) {
        if ($CircuitBreaker.enable -ne $true -or
            $CircuitBreaker.rollback -notin @($true, $false)) {
            throw 'Forward-only migration cutover requires an enabled ECS circuit breaker with an explicit rollback state.'
        }
        return
    }
    if (-not $AllowMissingRollback -and
        ($CircuitBreaker.enable -ne $true -or $CircuitBreaker.rollback -ne $true)) {
        throw 'Refusing deployment because the ECS deployment circuit breaker with rollback is not enabled. Configure it in a separately approved change or pass the explicit override after review.'
    }
}

function Merge-ContainerEnvironment {
    param([object]$Container, [System.Collections.IDictionary]$Desired, [string[]]$Remove)
    $existing = $Container.PSObject.Properties['environment']
    $retained = @($(if ($null -ne $existing) { $existing.Value }) | Where-Object {
        $name = [string]$_.name
        $name -notin $Remove -and -not $Desired.Contains($name) -and $name -notlike 'GEMINI_*' -and $name -notlike 'NOVAMIND_AUTH_*'
    })
    foreach ($entry in $Desired.GetEnumerator()) {
        $retained += [pscustomobject]@{ name = $entry.Key; value = $entry.Value }
    }
    Set-JsonProperty $Container 'environment' $retained
}

function Merge-ContainerSecrets {
    param([object]$Container, [string]$ApplicationSecretArn, [string[]]$Remove)
    $existing = $Container.PSObject.Properties['secrets']
    $existingSecrets = @($(if ($null -ne $existing) { $existing.Value }))
    foreach ($secret in $existingSecrets) {
        if ([string]$secret.name -match '(?i)(master|rds.*admin)') {
            throw 'The existing API task definition appears to reference a privileged database secret; remove it by reviewed migration before deployment.'
        }
    }
    $retained = @($existingSecrets | Where-Object {
        ([string]$_.name) -notin $Remove -and ([string]$_.name) -notlike 'NOVAMIND_AUTH_*'
    })
    $retained += [pscustomobject]@{ name = 'NOVAMIND_POSTGRES_SECRET'; valueFrom = $ApplicationSecretArn }
    Set-JsonProperty $Container 'secrets' $retained
}

if (-not $Apply) {
    Write-Output "Plan only: would deploy immutable image $ImageUri to ECS service $ClusterName/$ServiceName."
    $serviceAction = if ($CreateMissingService) { 'create the missing ECS service after registering its immutable revision' } else { 'update the existing ECS service' }
    Write-Output "It would $serviceAction and configure Cognito, the application-only RDS secret, Bedrock, and optional server-owned dispatcher identity using the reviewed egress mode: $EgressMode."
    if ($ForwardOnlyMigrationCutover) {
        Write-Output "Forward-only migration cutover is enabled for expected live source $ExpectedCurrentTaskDefinition; automatic rollback will be disabled and $HealthUrl plus ALB target health will be verified."
    }
    Write-Output 'PublicTaskEgress uses a task public IP only for outbound HTTPS while the task security group admits inbound traffic from the internal ALB only. ExistingNat and ApprovedEgressProxy require a verified private path for Cognito JWKS and Bedrock.'
    Write-Output 'No AWS, database, Secrets Manager, Docker, or HTTP request was made.'
    return
}
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw 'AWS CLI is required for -Apply.' }

$serviceJson = (& aws --profile $ProfileName --region $Region ecs describe-services --cluster $ClusterName --services $ServiceName `
    --query 'services[0].{TaskDefinition:taskDefinition,CircuitBreaker:deploymentConfiguration.deploymentCircuitBreaker,Network:networkConfiguration.awsvpcConfiguration,LoadBalancers:loadBalancers}' `
    --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the existing ECS service.' }
try { $service = if ($serviceJson -eq 'null' -or -not $serviceJson) { $null } else { $serviceJson | ConvertFrom-Json -ErrorAction Stop } } catch { throw 'ECS service metadata was invalid.' }
if ($null -eq $service -or -not $service.TaskDefinition) {
    if (-not $CreateMissingService) { throw 'The requested ECS service does not exist. Re-run with -CreateMissingService and reviewed bootstrap inputs.' }
    $sourceTaskDefinition = $BaseTaskDefinition
    $priorTaskDefinition = $null
} else {
    if ($CreateMissingService) { throw 'The requested ECS service already exists; refusing a second service creation.' }
    Assert-DeploymentCircuitBreakerSafety `
        -CircuitBreaker $service.CircuitBreaker `
        -IsForwardOnlyCutover ([bool]$ForwardOnlyMigrationCutover) `
        -AllowMissingRollback ([bool]$AllowNoDeploymentCircuitBreaker)
    if ($EgressMode -eq 'PublicTaskEgress' -and $service.Network.AssignPublicIp -ne 'ENABLED') {
        throw 'The existing ECS service is not configured for explicit public-task egress.'
    }
    if ($EgressMode -ne 'PublicTaskEgress' -and $service.Network.AssignPublicIp -eq 'ENABLED') {
        throw 'The existing ECS service has public-task egress but a private egress mode was selected.'
    }
    $sourceTaskDefinition = $service.TaskDefinition
    $priorTaskDefinition = $service.TaskDefinition
}
if ($ForwardOnlyMigrationCutover -and $service -and
    ([string]$service.TaskDefinition -notmatch ('(?:^|/)' + [regex]::Escape($ExpectedCurrentTaskDefinition) + '$'))) {
    throw "Forward-only cutover expected live task definition $ExpectedCurrentTaskDefinition; refusing a different source."
}

$definitionJson = (& aws --profile $ProfileName --region $Region ecs describe-task-definition --task-definition $sourceTaskDefinition `
    --query 'taskDefinition' --output json --no-cli-pager 2>$null | Out-String).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Unable to inspect the current ECS task definition.' }
try { $definition = $definitionJson | ConvertFrom-Json -ErrorAction Stop } catch { throw 'ECS task-definition metadata was invalid.' }
if (@($definition.requiresCompatibilities) -notcontains 'FARGATE' -or $definition.networkMode -ne 'awsvpc') {
    throw 'The API deployment helper only supports an existing Fargate awsvpc task definition.'
}
$containers = @($definition.containerDefinitions | Where-Object { [string]$_.name -eq $ContainerName })
if ($containers.Count -ne 1) { throw 'The requested API container name must match exactly one container definition.' }
$container = $containers[0]
Set-JsonProperty $container 'image' $ImageUri
Set-JsonProperty $container 'command' @('python', '-m', 'backend.container.ecs_entrypoint')
Set-JsonProperty $container 'readonlyRootFilesystem' $true

$desiredEnvironment = [ordered]@{
    'NOVAMIND_AUTH_MODE' = 'cognito'
    'NOVAMIND_COGNITO_ISSUER' = $CognitoIssuer
    'NOVAMIND_COGNITO_CLIENT_ID' = $CognitoClientId
    'NOVAMIND_COGNITO_CUSTOMER_GROUP' = $CognitoCustomerGroup
    'NOVAMIND_COGNITO_REVIEWER_GROUP' = $CognitoReviewerGroup
    'NOVAMIND_COGNITO_ADMIN_GROUP' = $CognitoAdminGroup
    'NOVAMIND_COGNITO_CUSTOMER_BINDINGS' = $CognitoCustomerBindingsJson
    'NOVAMIND_LLM_PROVIDER' = 'bedrock'
    'AWS_REGION' = $Region
    'BEDROCK_MODEL_ID' = $BedrockModelId
    'NOVAMIND_AGENT_ENGINE' = 'langgraph'
}
if ($EnableAutomaticDispatcher) {
    $desiredEnvironment['NOVAMIND_AUTOMATIC_DISPATCHER'] = 'true'
    $desiredEnvironment['NOVAMIND_DISPATCHER_USER_ID'] = $DispatcherUserId
    $desiredEnvironment['NOVAMIND_DISPATCHER_ROLE'] = $DispatcherRole
}
$removeEnvironment = @(
    'NOVAMIND_AUTH_CONTENT', 'NOVAMIND_AUTH_FILE', 'NOVAMIND_SQLITE_PATH', 'NOVAMIND_POSTGRES_DSN',
    'NOVAMIND_POSTGRES_SECRET', 'NOVAMIND_OFFLINE_TESTING', 'NOVAMIND_OFFLINE_DEMO_DATA',
    'NOVAMIND_BEDROCK_DEMO_DATA', 'NOVAMIND_AUTOMATIC_DISPATCHER', 'NOVAMIND_DISPATCHER_USER_ID',
    'NOVAMIND_DISPATCHER_ROLE', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
    'AWS_SHARED_CREDENTIALS_FILE'
)
Merge-ContainerEnvironment -Container $container -Desired $desiredEnvironment -Remove $removeEnvironment
Merge-ContainerSecrets -Container $container -ApplicationSecretArn $ApplicationSecretArn -Remove @(
    'NOVAMIND_AUTH_CONTENT', 'NOVAMIND_AUTH_FILE', 'NOVAMIND_SQLITE_PATH', 'NOVAMIND_POSTGRES_DSN',
    'NOVAMIND_POSTGRES_SECRET', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN'
)

foreach ($property in @('taskDefinitionArn', 'revision', 'status', 'requiresAttributes', 'compatibilities', 'registeredAt', 'registeredBy', 'deregisteredAt')) {
    $definition.PSObject.Properties.Remove($property)
}
$temporaryDefinition = New-TemporaryFile
try {
    [System.IO.File]::WriteAllText($temporaryDefinition, ($definition | ConvertTo-Json -Depth 100), [System.Text.UTF8Encoding]::new($false))
    $newTaskDefinition = (& aws --profile $ProfileName --region $Region ecs register-task-definition `
        --cli-input-json ("file://{0}" -f $temporaryDefinition) --query 'taskDefinition.taskDefinitionArn' `
        --output text --no-cli-pager 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $newTaskDefinition -notmatch '^arn:aws(?:-[a-z-]+)?:ecs:') {
        throw 'ECS refused the rendered task definition.'
    }
} finally {
    Remove-Item -LiteralPath $temporaryDefinition -Force -ErrorAction SilentlyContinue
}
if ($CreateMissingService) {
    $assignPublicIp = if ($EgressMode -eq 'PublicTaskEgress') { 'ENABLED' } else { 'DISABLED' }
    $network = 'awsvpcConfiguration={subnets=' + ($ApplicationPublicSubnetIds -join ',') +
        ',securityGroups=' + $ApiSecurityGroupId + ',assignPublicIp=' + $assignPublicIp + '}'
    $loadBalancer = 'targetGroupArn=' + $TargetGroupArn + ',containerName=' + $ContainerName + ',containerPort=8000'
    # Keep CLI diagnostics for a failed creation.  Do not redirect stderr to $null:
    # that turns actionable ECS validation errors into an opaque failure.  AWS CLI
    # errors are still redacted before they are surfaced by this helper.
    $createRollback = if ($ForwardOnlyMigrationCutover) { 'false' } else { 'true' }
    $createServiceOutput = @(& aws --profile $ProfileName --region $Region ecs create-service --cluster $ClusterName --service-name $ServiceName `
        --launch-type FARGATE --desired-count 1 --task-definition $newTaskDefinition --network-configuration $network `
        --load-balancers $loadBalancer --health-check-grace-period-seconds 60 `
        --deployment-configuration ("deploymentCircuitBreaker={enable=true,rollback=$createRollback},maximumPercent=200,minimumHealthyPercent=100") `
        --no-cli-pager 2>&1)
    $createServiceExitCode = $LASTEXITCODE
    if ($createServiceExitCode -ne 0) {
        $diagnostic = (($createServiceOutput | ForEach-Object { [string]$_ }) -join ' ').Trim()
        $diagnostic = $diagnostic -replace '(?i)\b(authorization|x-amz-security-token|x-amz-credential|aws_access_key_id|aws_secret_access_key|password|secret|token)\s*([=:])\s*[^\s,;]+', '$1$2[REDACTED]'
        $diagnostic = $diagnostic -replace '[\r\n\t]+', ' '
        if ($diagnostic.Length -gt 1500) { $diagnostic = $diagnostic.Substring(0, 1500) + ' [truncated]' }
        if (-not $diagnostic) { $diagnostic = 'No AWS CLI diagnostic was returned.' }
        throw "ECS CreateService failed (AWS CLI exit code $createServiceExitCode): $diagnostic"
    }
} else {
    $updateArgs = @('--profile', $ProfileName, '--region', $Region, 'ecs', 'update-service',
        '--cluster', $ClusterName, '--service', $ServiceName, '--task-definition', $newTaskDefinition)
    if ($ForwardOnlyMigrationCutover) {
        $updateArgs += @('--deployment-configuration', 'deploymentCircuitBreaker={enable=true,rollback=false},maximumPercent=200,minimumHealthyPercent=100')
    }
    $updateArgs += @('--no-cli-pager')
    & aws @updateArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw $(if ($ForwardOnlyMigrationCutover) { 'ECS refused the forward-only service update.' } else { 'ECS refused the service update.' })
    }
}
& aws --profile $ProfileName --region $Region ecs wait services-stable --cluster $ClusterName --services $ServiceName --no-cli-pager 2>$null
if ($LASTEXITCODE -ne 0) {
    if ($ForwardOnlyMigrationCutover) {
        Write-Output 'Forward-only migration cutover did not stabilize. Automatic rollback is disabled; inspect migrations, sanitized logs, ECS events, and ALB health, then advance with a compatible revision.'
    } elseif ($null -ne $priorTaskDefinition) {
        Write-Output "Rollback candidate (review before use): aws --profile $ProfileName --region $Region ecs update-service --cluster $ClusterName --service $ServiceName --task-definition $priorTaskDefinition --no-cli-pager"
    } else {
        Write-Output 'Initial service creation did not stabilize. The ECS deployment circuit breaker rolls back failed tasks; inspect ECS events before any reviewed cleanup.'
    }
    throw 'The ECS service did not stabilize. Inspect ECS events and target health; do not rerun bootstrap or create another database secret.'
}
if ($ForwardOnlyMigrationCutover) {
    $targetGroupArn = if ($service -and @($service.LoadBalancers).Count -eq 1) { [string]$service.LoadBalancers[0].targetGroupArn } else { $null }
    if (-not $targetGroupArn) { throw 'Forward-only migration cutover could not identify the existing ALB target group.' }
    $targetJson = (& aws --profile $ProfileName --region $Region elbv2 describe-target-health --target-group-arn $targetGroupArn `
        --query 'TargetHealthDescriptions[].TargetHealth.State' --output json --no-cli-pager 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Unable to verify ALB target health after the forward-only cutover.' }
    $targetStates = $targetJson | ConvertFrom-Json
    if (@($targetStates).Count -lt 1 -or @($targetStates | Where-Object { $_ -ne 'healthy' }).Count -ne 0) {
        throw 'Forward-only migration cutover did not produce all-healthy ALB targets.'
    }
    try {
        $health = Invoke-WebRequest -UseBasicParsing -TimeoutSec 10 -Uri $HealthUrl
        if ($health.StatusCode -ne 200 -or $health.Content -notmatch '"status"\s*:\s*"ok"') {
            throw 'health contract mismatch'
        }
    } catch {
        throw 'Forward-only migration cutover API health verification failed.'
    }
}
if ($null -ne $priorTaskDefinition) {
    if ($ForwardOnlyMigrationCutover) {
        Write-Output "Forward-only ECS service stabilized on $newTaskDefinition. Revision $priorTaskDefinition is not an automatic rollback candidate after migration 13."
    } else {
        Write-Output "ECS service stabilized on $newTaskDefinition. The prior revision remains the reviewed rollback candidate."
    }
} else {
    Write-Output "ECS service created and stabilized on $newTaskDefinition. Future revisions retain this task definition as the rollback candidate."
}
