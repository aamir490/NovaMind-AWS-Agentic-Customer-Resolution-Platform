# NovaMind domain-free AWS development deployment

This runbook uses the existing CloudFront URL
`https://d2pgtx2d6hm04w.cloudfront.net`, its AWS-managed certificate, and an
AWS-provided Cognito Hosted UI domain. It does not require a registered domain,
Route 53 record, ACM certificate, NAT gateway, or VPC endpoint. Every helper
is plan-only until `-Apply`; the CloudFormation helper creates a reviewable
change set but never executes one.

The approved dev topology is: browser HTTPS → CloudFront → VPC Origin →
internal ALB HTTP :80 → Fargate :8000. The ALB lives in new dedicated private
subnets. The one Fargate task uses a public IP only for outbound HTTPS to the
public Cognito JWKS endpoint and Bedrock; its security group accepts inbound
traffic exclusively from the internal ALB. RDS stays private and TLS-verified.

## Additive ECR and ECS foundation order

Create and review these opt-in stacks before registering the 6G2 task
definitions. Both templates default creation to `false` and create no services,
tasks, secrets, networking, or image content.

1. Create `novamind-crp-dev-ecr` with `CreateEcrRepository=true`. Record the
   exported `novamind-crp-dev-BackendRepositoryUri` and push only an approved
   immutable image tag/digest through the normal release process.
2. Create `novamind-crp-dev-ecs-cluster` with `CreateEcsCluster=true`. Record
   the exported `novamind-crp-dev-EcsClusterArn` and
   `novamind-crp-dev-EcsClusterName`.
3. Deploy the 6F messaging foundation, bootstrap support, and 6G2 services
   only after inspecting the two change sets and confirming their imported
   names/ARNs match these exports.

The ECR template enables scan-on-push, immutable tags, AES-256 repository
encryption, and lifecycle retention. The ECS cluster enables CloudWatch
container insights. Neither template reads or creates a secret.

## Approval 1 — Cognito, private origin, and image

First create and inspect two CloudFormation change sets. Confirm the proposed
private-subnet CIDRs do not overlap any subnet created since the read-only
inventory: `172.31.112.0/24` in `us-east-1a` and `172.31.113.0/24` in
`us-east-1b`. The network stack creates no service yet; the deploy helper does
that after RDS bootstrap succeeds.

```powershell
$root = 'E:\codex-projetc\novamind-crp'
$profile = 'novamind'; $region = 'us-east-1'
Set-Location $root

.\scripts\New-NovaMindCloudFormationChangeSet.ps1 -StackName novamind-cognito-dev -ChangeSetName kiro-cognito-domain-free-001 -ChangeSetType CREATE -TemplatePath .\infrastructure\novamind-cognito-components.template.json -ParametersPath C:\secure-operator-input\cognito-parameters.json -ProfileName $profile -Region $region -Apply
.\scripts\New-NovaMindCloudFormationChangeSet.ps1 -StackName novamind-crp-api-vpc-origin-repair-dev -ChangeSetName novamind-crp-api-vpc-origin-repair-20261027 -ChangeSetType CREATE -TemplatePath .\infrastructure\novamind-ecs-network-service.template.json -ParametersPath .\infrastructure\novamind-crp-api-vpc-origin-repair.parameters.json -ProfileName $profile -Region $region -Apply
```

For this account, the network parameter file must reference VPC
`vpc-0baf042257d729f7d`, VPC CIDR `172.31.0.0/16`, RDS SG
`sg-02b84e9e7f6b9f594`, CloudFront prefix list `pl-3b927c52`, existing API-task
SG `sg-0726ae466e736b514`, and `ApplicationPublicSubnetIds` set to
`subnet-0405745b30f4764a0,subnet-0643d7235c890cfe6`. Set
`CreateOriginPrivateSubnets`, `CreateLoadBalancerSecurityGroup`,
`CreateInternalApiLoadBalancer`, `CreateApiTargetGroup`,
`CreateInternalApiHttpListener`, and `CreateCloudFrontVpcOrigin` to `true`.
Set `CreateApiSecurityGroup=false`, `AddApiIngressFromLoadBalancer=true`,
`AddRdsIngressFromApi=false`, `ApiHttpsEgressReady=false`,
`EnableApiExternalHttpsEgress=false`, `ApiPublicIpEgress=false`, and
`CreateApiService=false`. Do not put secret values in either parameter file.

When the listed origin/ALB `Create*` switches above are `true`, omit these inactive
parameters entirely (or pass an empty string):
`ExistingOriginPrivateSubnetAId`, `ExistingOriginPrivateSubnetBId`, and
`ExistingLoadBalancerSecurityGroupId`. Because `CreateApiSecurityGroup=false`,
`ExistingApiSecurityGroupId` must be the verified real value above. Never pass
`subnet-00000000` or `sg-00000000`; the Change Set helper rejects those values
and the template now uses empty defaults for inactive existing-resource
parameters. A `ROLLBACK_COMPLETE` stack cannot accept a new CREATE change set;
use the reviewed command only after the failed stack has been handled through
the approved stack-replacement procedure or under a new approved stack name.

For Cognito, use `ProjectName=novamind-crp`, set only the required `Create*`
switches to `true`, and select exactly one pool source. For the first dev
deployment, the reviewed set is the new pool, the Admin and Customer groups, one public
SPA client, the Hosted UI domain, and `CreateConfigurationSecret=true`.
The pool requires software-token MFA for all roles because Cognito MFA is
pool-wide; do not treat group membership as privileged MFA evidence.
Set callback/logout URLs to `https://d2pgtx2d6hm04w.cloudfront.net/`, select a
globally unique Hosted UI prefix, and pass only approved subject-to-customer
bindings in `CustomerBindingsJson`. The configuration secret contains no
client secret or password; it is the `CognitoConfigurationSecretArn` consumed
by the API task. Record the pool ID, SPA client ID, issuer, configuration
secret ARN, and Hosted UI domain from the approved stack outputs. Execute only
the reviewed change sets, then build
and push the current source under a new immutable image tag:

```powershell
.\scripts\Publish-NovaMindBackendImage.ps1 -RepositoryUri '357001292388.dkr.ecr.us-east-1.amazonaws.com/novamind-crp-backend-dev' -ImageTag 'APPROVED_RELEASE' -SourceRevision 'APPROVED_SOURCE_REVISION' -ExistingLocalImage 'novamind-crp-backend:ca-verified' -ProfileName $profile -Region $region -Apply
```

## Approval 2 — application database secret and one-shot bootstrap

First create and inspect the secret-free bootstrap-network change set. It is
limited to the dedicated zero-ingress group and one additive RDS ingress rule;
it must not create a task, secret, database, NAT, or service.

```powershell
.\scripts\New-NovaMindCloudFormationChangeSet.ps1 -StackName novamind-rds-bootstrap-network-dev -ChangeSetName kiro-rds-bootstrap-network-001 -ChangeSetType CREATE -TemplatePath .\infrastructure\novamind-rds-bootstrap-network.template.json -ParametersPath .\infrastructure\novamind-rds-bootstrap-network.parameters.dev.json -ProfileName $profile -Region $region -Apply
```

Create the separate application secret from a reviewed RDS CA bundle. Create
and inspect the bootstrap-support change set using the master-secret ARN and
the new application-secret ARN, but never a secret value. It must grant the
master-secret read only to the one-shot bootstrap role, app-secret read only to
`novamind-crp-ecs-execution-role-dev`, and selected Bedrock invocation only to
`novamind-crp-ecs-task-role-dev`.

For an existing bootstrap-support stack, prepare a reviewed JSON snapshot of
its deployed `Parameters` array outside the repository. The Windows PowerShell
5.1-safe helper preserves every individual existing `ParameterKey` with
`UsePreviousValue=true` and adds only the execution-role fields plus the
verified ECR repository ARN. It accepts either a one-item or multi-item JSON
array; do not flatten names into a comma-delimited key.

```powershell
$caBundle = 'C:\secure-operator-input\rds-global-bundle.pem'
.\scripts\New-NovaMindRdsApplicationSecret.ps1 -RdsCaBundlePath $caBundle -ProfileName $profile -Region $region -Apply

.\scripts\New-NovaMindCloudFormationChangeSet.ps1 -StackName novamind-rds-bootstrap-dev -ChangeSetName kiro-rds-bootstrap-001 -ChangeSetType CREATE -TemplatePath .\infrastructure\novamind-rds-bootstrap-support.template.json -ParametersPath C:\secure-operator-input\bootstrap-parameters.json -ProfileName $profile -Region $region -Apply

.\scripts\New-NovaMindBootstrapExecutionRoleChangeSet.ps1 `
  -StackName novamind-crp-dev-bootstrap-support `
  -ChangeSetName novamind-crp-bootstrap-execution-dev-001 `
  -ExistingParametersPath C:\secure-operator-input\bootstrap-existing-parameters.json `
  -BootstrapEcrRepositoryArn 'arn:aws:ecr:us-east-1:ACCOUNT:repository/novamind-crp-backend-dev' `
  -ProfileName $profile -Region $region -Apply

.\scripts\Start-NovaMindRdsBootstrap.ps1 -MasterSecretArn 'RDS_MANAGED_MASTER_SECRET_ARN' -ApplicationSecretArn 'NOVAMIND_APPLICATION_SECRET_ARN' -BackendImageUri 'ECR_IMAGE_AT_SHA256_DIGEST' -RdsEndpoint 'RDS_ENDPOINT_ADDRESS' -BootstrapTaskRoleArn 'BOOTSTRAP_TASK_ROLE_ARN' -ExecutionRoleArn 'arn:aws:iam::357001292388:role/novamind-crp-bootstrap-execution-dev' -ClusterName novamind-crp-ecs-cluster-dev -SubnetId @('ECS_PRIVATE_SUBNET_A','ECS_PRIVATE_SUBNET_B') -BootstrapSecurityGroupId 'BOOTSTRAP_STACK_SECURITY_GROUP_ID' -CloudWatchLogGroup /ecs/novamind-rds-bootstrap -ProfileName $profile -Region $region -Apply
```

The helper prints only parameter count and names before the AWS call. It
requires the existing stack snapshot, validates the template and JSON root,
and never reads a secret value. Inspect the Change Set before execution; the
helper never executes it.

The bootstrap task must use a dedicated zero-ingress security group in the
two verified ECS private subnets, with `assignPublicIp=DISABLED` and an active
NAT-backed default route. Its only egress rules are PostgreSQL to the RDS
security group and TCP `443` for the required AWS APIs. Do not add public IPs,
additional public ports, or any API-SG rule. It runs migrations with
`sslmode=verify-full` and writes connection data to the app secret only after
success. Do not roll back migrations or make a second app secret after failure.

## Production database preflight (read-only; separate approval)

Before preparing the forward-only evidence file, run
`scripts/Read-NovaMindProductionMigrationPreflight.py`. It uses only the
injected application secret and `sslmode=verify-full`; it does not run the
application's migration or schema-version validator. The query checks the
`novamindcrp` database, `novamind` schema, `novamind_app` login identity,
schema USAGE/CREATE privileges, table/function ownership, and a contiguous
migration ledger with current version 7, 8, or 9. Output is limited to a
single PASS record or a phase code (`SECRET_ENVIRONMENT_MISSING`,
`SECRET_PARSE_INVALID`, `DATABASE_CONNECTION`, or `RESULT_VALIDATION`).

If an already authorized read-only `psql` connection exists, use the SQL
contract directly without placing credentials in the evidence file:

```powershell
$env:PGSSLMODE = 'verify-full'
$env:PGOPTIONS = '-c search_path=novamind,pg_catalog'
psql --no-psqlrc --set=ON_ERROR_STOP=1 --file .\scripts\Read-NovaMindProductionMigrationPreflight.sql
```

No such connection is assumed. The approved ECS alternative is exactly one
temporary `run-task` using task definition `novamind-crp-backend-dev:5`, the
existing application-secret injection, existing validated networking, and a
command override that executes the diagnostic source (base64-embedded as
`python -c`). The override must not invoke bootstrap, migrations, Bedrock, or
the service command. Use `--cli-input-json` and inspect the task exit code and
sanitized log output; do not retry. The generated PASS fields are the only
values copied into the operator evidence file.

### Customer and Cognito binding inspection

The image-contained `backend.container.customer_inspection` module is a
separate, one-shot diagnostic. It uses the existing API task definition's
project application-secret projection, validates the fixed RDS CA file, sets
the PostgreSQL session read-only, and queries only
`novamind.customers` and `novamind.cognito_customer_bindings`. It emits bounded
IDs, names, bindings, and row counts only; it never starts FastAPI, runs
migrations, or prints connection material.

Prepare the following command with the currently reviewed API task-definition
revision, private subnet IDs, and API security group. Do not execute it until
the task definition, network path, RDS identity, and log destination are
independently reviewed:

```powershell
$overrides = '{"containerOverrides":[{"name":"backend","command":["python","-m","backend.container.customer_inspection"]}]}'
aws --profile novamind --region us-east-1 ecs run-task `
  --cluster novamind-crp-ecs-cluster-dev `
  --task-definition novamind-crp-backend-dev:<REVIEWED_API_REVISION> `
  --launch-type FARGATE `
  --network-configuration 'awsvpcConfiguration={subnets=[subnet-0ef827a350fddfba6,subnet-05963f195c4bdbbe5a],securityGroups=[sg-0726ae466e736b514],assignPublicIp=DISABLED}' `
  --overrides $overrides `
  --enable-ecs-managed-tags `
  --tags key=Project,value=novamind-crp key=Environment,value=dev key=ManagedBy,value=CloudFormation `
  --no-cli-pager
```

Run exactly once. Capture only the task ARN, stopped reason, exit code, and
sanitized diagnostic JSON. Do not retry automatically. After verification,
retain no task-specific artifacts; no API service task is changed. A failed
task requires operator review before any further action.

## Approval 3 — forward-only durable-failure migration cutover

This cutover is opt-in and must use the live ECS task definition
`novamind-crp-backend-dev:5` as its source. Do not use the stale `:3` or `:4`
service examples. AUTO remains disabled: do not pass
`-EnableAutomaticDispatcher`, `-DispatcherUserId`, or `-DispatcherRole`.

Before approval, create a short-lived operator evidence file (outside the
repository and without credentials) proving the production read-only checks:

```json
{
  "Database": "novamindcrp",
  "Schema": "novamind",
  "ApplicationRole": "novamind_app",
  "SchemaOwner": "novamind_app",
  "DdlPermissions": true,
  "CurrentSchemaVersion": 13,
  "TargetSchemaVersion": 13,
  "VerifiedAtUtc": "<fresh UTC timestamp>"
}
```

The file is consumed only by the fail-closed deployment preflight. It must be
fresh (less than 24 hours old), and the database/role/schema values must match
exactly. The production query must be read-only; never put a DSN or secret in
this file.

`-ForwardOnlyMigrationCutover` changes the ECS service deployment configuration
to `rollback=false` in the same service update that selects the new revision.
This prevents ECS from automatically restoring a pre-migration revision after migration 13.
If the rollout fails, leave the service in place, inspect sanitized logs,
migration state, ECS events and ALB health, then advance with a compatible
revision. Do not issue a rollback to revision 5.

```powershell
.\scripts\Deploy-NovaMindEcsApi.ps1 -ClusterName novamind-crp-ecs-cluster-dev -ServiceName novamind-crp-api-dev -ContainerName backend -ImageUri 'ECR_IMAGE_AT_SHA256_DIGEST' -ApplicationSecretArn 'NOVAMIND_APPLICATION_SECRET_ARN' -CognitoIssuer 'COGNITO_ISSUER_OUTPUT' -CognitoClientId 'COGNITO_SPA_CLIENT_ID_OUTPUT' -CognitoCustomerGroup customers -CognitoReviewerGroup reviewers -CognitoAdminGroup admins -BedrockModelId 'APPROVED_BEDROCK_MODEL_OR_INFERENCE_PROFILE' -EgressMode PublicTaskEgress -CognitoCustomerBindingsJson '[]' -ExpectedCurrentTaskDefinition novamind-crp-backend-dev:5 -MigrationPreflightEvidencePath 'C:\secure-operator-input\novamind-production-migration-preflight.json' -HealthUrl 'https://d2pgtx2d6hm04w.cloudfront.net/api/health' -ForwardOnlyMigrationCutover -ProfileName $profile -Region $region -Apply

.\scripts\Configure-NovaMindCloudFrontApiOrigin.ps1 -DistributionId E1MBA2UCNSORUF -ApiOriginId novamind-api-vpc -VpcOriginId 'NETWORK_STACK_VPC_ORIGIN_ID' -InternalAlbArn 'NETWORK_STACK_INTERNAL_ALB_ARN' -InternalAlbDnsName 'NETWORK_STACK_INTERNAL_ALB_DNS_NAME' -CachingDisabledPolicyId 4135ea2d-6df8-44a3-9df3-4b5a84be39ad -AuthorizationOriginRequestPolicyId b689b0a8-53d0-40ab-baf2-68738e2966ac -AddMissingApiRoute -ProfileName $profile -Apply

.\scripts\Publish-NovaMindFrontend.ps1 -BucketName 'EXISTING_PRIVATE_FRONTEND_BUCKET' -DistributionId E1MBA2UCNSORUF -CognitoDomain 'COGNITO_HOSTED_UI_DOMAIN_OUTPUT' -CognitoClientId 'COGNITO_SPA_CLIENT_ID_OUTPUT' -CallbackUri 'https://d2pgtx2d6hm04w.cloudfront.net/' -LogoutUri 'https://d2pgtx2d6hm04w.cloudfront.net/' -ProfileName $profile -Region $region -Apply

.\scripts\Test-NovaMindAwsRelease.ps1 -ClusterName novamind-crp-ecs-cluster-dev -ServiceName novamind-crp-api-dev -TargetGroupArn 'NETWORK_STACK_API_TARGET_GROUP_ARN' -DistributionId E1MBA2UCNSORUF -CloudFrontUrl 'https://d2pgtx2d6hm04w.cloudfront.net/' -ApiOriginId novamind-api-vpc -VpcOriginId 'NETWORK_STACK_VPC_ORIGIN_ID' -ExpectedInternalAlbArn 'NETWORK_STACK_INTERNAL_ALB_ARN' -ExpectedInternalAlbDnsName 'NETWORK_STACK_INTERNAL_ALB_DNS_NAME' -CachingDisabledPolicyId 4135ea2d-6df8-44a3-9df3-4b5a84be39ad -AuthorizationOriginRequestPolicyId b689b0a8-53d0-40ab-baf2-68738e2966ac -ProfileName $profile -Region $region -Apply
```

The CloudFront helper refuses a public custom origin and verifies that the VPC
Origin points to the reviewed internal-ALB ARN. It adds exactly one VPC origin
and `/api/*` behavior only with `-AddMissingApiRoute`, forwards
`Authorization` and all query strings, permits API write methods, and disables
API caching. Roll back a later ECS update to the printed task definition; for a
CloudFront rollback, fetch a new ETag and restore the prior reviewed
distribution configuration. Finish with one authorized manual investigation,
confirm the durable failure fields are populated on a controlled failure, and
verify the existing HITL reviewer path. Keep AUTO disabled until the manual
Bedrock path has passed independently and a server-only reviewer identity is
approved.
