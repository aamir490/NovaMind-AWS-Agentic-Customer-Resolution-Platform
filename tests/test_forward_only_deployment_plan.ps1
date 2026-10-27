$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts/Deploy-NovaMindEcsApi.ps1'
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $scriptPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw 'Deployment script did not parse.' }
$functionAst = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Assert-DeploymentCircuitBreakerSafety'
}, $true)
if ($null -eq $functionAst) { throw 'Circuit-breaker safety function was not found.' }
. ([scriptblock]::Create($functionAst.Extent.Text))

foreach ($rollbackState in @($true, $false)) {
    Assert-DeploymentCircuitBreakerSafety `
        -CircuitBreaker ([pscustomobject]@{ enable = $true; rollback = $rollbackState }) `
        -IsForwardOnlyCutover $true `
        -AllowMissingRollback $false
}
Assert-DeploymentCircuitBreakerSafety `
    -CircuitBreaker ([pscustomobject]@{ enable = $true; rollback = $true }) `
    -IsForwardOnlyCutover $false `
    -AllowMissingRollback $false
$normalRejected = $false
try {
    Assert-DeploymentCircuitBreakerSafety `
        -CircuitBreaker ([pscustomobject]@{ enable = $true; rollback = $false }) `
        -IsForwardOnlyCutover $false `
        -AllowMissingRollback $false
} catch {
    $normalRejected = $true
}
if (-not $normalRejected) { throw 'Normal deployment accepted rollback=false without its explicit override.' }
foreach ($unsafeState in @(
    [pscustomobject]@{ enable = $false; rollback = $false },
    [pscustomobject]@{ enable = $true; rollback = $null }
)) {
    $rejected = $false
    try {
        Assert-DeploymentCircuitBreakerSafety -CircuitBreaker $unsafeState `
            -IsForwardOnlyCutover $true -AllowMissingRollback $false
    } catch {
        $rejected = $true
    }
    if (-not $rejected) { throw 'Unsafe forward-only circuit-breaker state was accepted.' }
}
$evidence = New-TemporaryFile
try {
    [ordered]@{
        Database = 'novamindcrp'
        Schema = 'novamind'
        ApplicationRole = 'novamind_app'
        SchemaOwner = 'novamind_app'
        DdlPermissions = $true
        CurrentSchemaVersion = 13
        TargetSchemaVersion = 13
        VerifiedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $evidence -Encoding utf8

    $parameters = @{
        ClusterName = 'novamind-crp-ecs-cluster-dev'
        ServiceName = 'novamind-crp-api-dev'
        ContainerName = 'backend'
        ImageUri = ('357001292388.dkr.ecr.us-east-1.amazonaws.com/novamind-crp-backend-dev@sha256:' + ('a' * 64))
        ApplicationSecretArn = 'arn:aws:secretsmanager:us-east-1:000000000000:secret:app'
        CognitoIssuer = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_NdlzVw24C'
        CognitoClientId = 'client'
        CognitoCustomerGroup = 'customers'
        CognitoReviewerGroup = 'reviewers'
        CognitoAdminGroup = 'admins'
        BedrockModelId = 'us.amazon.nova-lite-v1:0'
        EgressMode = 'PublicTaskEgress'
        CognitoCustomerBindingsJson = '[]'
        ExpectedCurrentTaskDefinition = 'novamind-crp-backend-dev:5'
        MigrationPreflightEvidencePath = $evidence.FullName
        HealthUrl = 'https://example.invalid/api/health'
        ForwardOnlyMigrationCutover = $true
    }
    $output = & $scriptPath @parameters
    if (($LASTEXITCODE -and $LASTEXITCODE -ne 0) -or ($output -join "`n") -notmatch 'Forward-only migration cutover is enabled') {
        throw 'Valid forward-only plan was rejected.'
    }

    foreach ($unsupportedEvidence in @(
        @{ CurrentSchemaVersion = 12; TargetSchemaVersion = 13 },
        @{ CurrentSchemaVersion = 13; TargetSchemaVersion = 12 }
    )) {
        [ordered]@{
            Database = 'novamindcrp'
            Schema = 'novamind'
            ApplicationRole = 'novamind_app'
            SchemaOwner = 'novamind_app'
            DdlPermissions = $true
            CurrentSchemaVersion = $unsupportedEvidence.CurrentSchemaVersion
            TargetSchemaVersion = $unsupportedEvidence.TargetSchemaVersion
            VerifiedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
        } | ConvertTo-Json | Set-Content -LiteralPath $evidence -Encoding utf8
        $rejected = $false
        try {
            & $scriptPath @parameters 2>$null
        } catch {
            $rejected = $true
        }
        if (-not $rejected) { throw 'An unsupported migration source or target version was accepted.' }
    }

    $parameters.MigrationPreflightEvidencePath = $evidence.FullName
    [ordered]@{
        Database = 'novamindcrp'
        Schema = 'novamind'
        ApplicationRole = 'novamind_app'
        SchemaOwner = 'novamind_app'
        DdlPermissions = $true
        CurrentSchemaVersion = 13
        TargetSchemaVersion = 13
        VerifiedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $evidence -Encoding utf8

    $invalid = [IO.Path]::GetTempFileName()
    try {
        '{"Database":"wrong","DdlPermissions":false}' | Set-Content -LiteralPath $invalid -Encoding utf8
        $invalidParameters = @{} + $parameters
        $invalidParameters.MigrationPreflightEvidencePath = $invalid
        $rejected = $false
        try {
            & $scriptPath @invalidParameters 2>$null
        } catch {
            $rejected = $true
        }
        if (-not $rejected -and -not ($LASTEXITCODE -and $LASTEXITCODE -ne 0)) { throw 'Invalid migration evidence was accepted.' }
    } finally {
        Remove-Item -LiteralPath $invalid -Force -ErrorAction SilentlyContinue
    }
    Write-Output 'FORWARD_ONLY_PLAN_TEST_PASS'
} finally {
    Remove-Item -LiteralPath $evidence -Force -ErrorAction SilentlyContinue
}
