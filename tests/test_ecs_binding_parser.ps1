# Offline regression test for the ECS deployment helper's JSON-array binding input.
# The helper remains in plan-only mode, so this test never invokes AWS.

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$workspace = Split-Path -Parent $PSScriptRoot
$helper = Join-Path $workspace 'scripts/Deploy-NovaMindEcsApi.ps1'
$common = @{
    ClusterName = 'novamind-crp-ecs-cluster-dev'
    ServiceName = 'novamind-crp-api-dev'
    ContainerName = 'backend'
    ImageUri = '357001292388.dkr.ecr.us-east-1.amazonaws.com/novamind-crp-backend-dev@sha256:0000000000000000000000000000000000000000000000000000000000000000'
    ApplicationSecretArn = 'arn:aws:secretsmanager:us-east-1:357001292388:secret:novamind-crp-postgres-app-dev-N58XRA'
    CognitoIssuer = 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_NdlzVw24C'
    CognitoClientId = '12n4ah3fpumi2di0e83acid747'
    CognitoCustomerGroup = 'customers'
    CognitoReviewerGroup = 'reviewers'
    CognitoAdminGroup = 'admins'
    BedrockModelId = 'us.amazon.nova-lite-v1:0'
    EgressMode = 'PublicTaskEgress'
    Region = 'us-east-1'
    ProfileName = 'novamind'
}

function Assert-PlanAccepts([string]$name, [string]$bindings) {
    try {
        $output = & $helper @common -CognitoCustomerBindingsJson $bindings 2>&1
    } catch {
        throw "$name should be accepted: $($_.Exception.Message)"
    }
    if (($output -join "`n") -notmatch 'Plan only:') {
        throw "$name did not reach plan-only mode."
    }
}

function Assert-PlanRejects([string]$name, [string]$bindings) {
    try {
        $null = & $helper @common -CognitoCustomerBindingsJson $bindings 2>&1
    } catch {
        if ($_.Exception.Message -eq 'CognitoCustomerBindingsJson must be a bounded JSON array.') { return }
        throw "$name returned an unexpected error: $($_.Exception.Message)"
    }
    throw "$name should have been rejected."
}

$first = '{"subject_id":"8408a468-9021-7008-d525-9ae83b993182","customer_id":"887d7598-31fa-446f-8e35-7c26a95dc1d1"}'
$second = '{"subject_id":"22222222-2222-4222-8222-222222222222","customer_id":"33333333-3333-4333-8333-333333333333"}'

Assert-PlanAccepts 'empty array' '[]'
Assert-PlanAccepts 'single-item array' "[$first]"
Assert-PlanAccepts 'multiple-item array' "[$first,$second]"
Assert-PlanRejects 'malformed JSON' '[}'
Assert-PlanRejects 'non-array JSON' $first

Write-Output 'ECS_BINDING_PARSER_TEST_PASS'
