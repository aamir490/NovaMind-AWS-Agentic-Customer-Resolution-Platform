# Private RDS PostgreSQL initialization

This is a deliberately narrow initialization path for the existing private RDS
instance `novamind-crp-postgres-dev` and database `novamindcrp`. It creates
one application login, `novamind_app`, which owns only the `novamind` schema.
The one-time ECS task reads the RDS-managed master secret; the permanent API
task never receives permission to read it.

Nothing in this guide migrates SQLite, creates RDS, changes Cognito, or changes
the human-approval workflow. The initializer runs the checked-in PostgreSQL
migrations, including the immutable audit, job-lease, customer-binding, and
LangGraph checkpoint protections, as `novamind_app`.

## Files and secret shape

| Purpose | File |
| --- | --- |
| One-time task code | `backend/container/rds_bootstrap.py` |
| Strict ECS RDS secret/TLS parser | `backend/container/rds_config.py` |
| Bootstrap task definition | `infrastructure/novamind-rds-bootstrap-task-definition.template.json` |
| Bootstrap task policy | `infrastructure/novamind-rds-bootstrap-task-role-policy.template.json` |
| Permanent backend execution-role policy | `infrastructure/novamind-rds-backend-execution-role-policy.template.json` |
| Permanent backend container secret fragment | `infrastructure/novamind-rds-backend-container-fragment.template.json` |
| Local-only / explicit-apply helpers | `scripts/New-NovaMindRdsApplicationSecret.ps1`, `scripts/Start-NovaMindRdsBootstrap.ps1` |

The initial application secret contains only `username`, a Secrets
Manager-generated `password`, and the fixed trusted CA-file path
`rds_ca_path`. The reviewed global RDS CA bundle is supplied separately in
the bootstrap/runtime image at `/etc/novamind/rds-ca-bundle.pem`; it is never
embedded in Secrets Manager. On success the initializer replaces the seed
with the fixed runtime shape below. Do not add arbitrary fields; startup
rejects them.

The backend image obtains the bundle only during an explicitly connected image
build from the AWS RDS trust-store URL. The build requires the reviewed
64-character `RDS_CA_BUNDLE_SHA256` argument, verifies the downloaded bytes and
PEM contents, and installs a root-owned read-only file. Builds without the
digest fail closed; no runtime container downloads certificates.

### Reproducible CA digest review

Do not invent or copy a digest from an old build. On an approved PowerShell
workstation, run the following as one review session. It downloads only the
public AWS trust-store object, keeps it outside the repository, prints the
SHA-256, and displays every certificate subject, issuer, expiry, and thumbprint
for operator review:

```powershell
$caUrl = 'https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem'
$caFile = Join-Path $env:TEMP 'novamind-rds-global-bundle.pem'
Invoke-WebRequest -Uri $caUrl -OutFile $caFile -UseBasicParsing
$pem = [System.IO.File]::ReadAllText($caFile, [System.Text.Encoding]::UTF8)
if (-not $pem.Contains('-----BEGIN CERTIFICATE-----') -or
    -not $pem.Contains('-----END CERTIFICATE-----')) { throw 'The downloaded file is not a PEM bundle.' }
$blocks = [regex]::Matches($pem, '(?s)-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----')
if ($blocks.Count -eq 0) { throw 'The bundle contains no certificates.' }
$blocks | ForEach-Object {
    [System.Security.Cryptography.X509Certificates.X509Certificate2]::CreateFromPem($_.Value) |
        Select-Object Subject, Issuer, NotAfter, Thumbprint
}
$sha256 = (Get-FileHash -LiteralPath $caFile -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Output "RDS_CA_BUNDLE_SHA256=$sha256"
Write-Output "RDS_CA_BUNDLE_BYTES=$([System.IO.File]::Length($caFile))"

# After reviewing the URL, certificate list, expiry dates, byte count, and digest:
docker build --file backend/Dockerfile --build-arg "RDS_CA_BUNDLE_SHA256=$sha256" backend
```

The Docker build independently downloads the same URL, recomputes the digest,
validates the PEM, and fails if the bytes differ. Record the reviewed digest
with the immutable image build metadata; never put the PEM or digest in a
secret, task environment, or application secret.

```json
{
  "username": "novamind_app",
  "password": "generated and never printed",
  "host": "RDS endpoint from the master secret",
  "port": 5432,
  "dbname": "novamindcrp",
  "schema": "novamind",
  "rds_ca_path": "/etc/novamind/rds-ca-bundle.pem"
}
```

The API startup path uses this secret only when `NOVAMIND_POSTGRES_SECRET` is
present. It writes the CA chain with mode `0600` to container tmpfs and builds a
DSN with `sslmode=verify-full`, `sslrootcert`, and the fixed search path
`novamind,pg_catalog`. The existing `NOVAMIND_POSTGRES_DSN` remains unchanged
for SQLite-compatible local work and Phase C tests.

## Ordered PowerShell deployment guide

Run these commands only from an approved operator workstation after reviewing
the account, role names, image digest, VPC, and RDS change window. They are
deployment instructions, not commands run by this repository setup.

1. Build and push an image containing these files using the normal reviewed
   NovaMind image process. Use an immutable image digest in the next steps.
   Obtain the current AWS RDS CA bundle from the AWS RDS trust-store guidance,
   review its checksum, and save it outside the repository, for example:

   ```powershell
   $root = 'E:\codex-projetc\NovaMind-local-smoke-test'
   $profile = 'novamind'
   $region = 'us-east-1'
   $caBundle = 'C:\secure-operator-input\rds-global-bundle.pem'
   ```

2. Obtain only the master-secret ARN from RDS metadata; do not retrieve the
   secret value. Confirm that the result is the RDS-managed master secret for
   the named instance.

   ```powershell
   $masterArn = aws --profile $profile --region $region rds describe-db-instances `
     --db-instance-identifier novamind-crp-postgres-dev `
     --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text --no-cli-pager
   if ($masterArn -notmatch '^arn:aws.*:secretsmanager:') { throw 'RDS has no managed master secret ARN.' }
   ```

3. Create a new, separate application seed secret. The helper defaults to plan
   mode; `-Apply` is required to call Secrets Manager. Its output is an ARN,
   not a password.

   ```powershell
   Set-Location $root
   .\scripts\New-NovaMindRdsApplicationSecret.ps1 -RdsCaBundlePath $caBundle `
     -ProfileName $profile -Region $region -Apply
   # Copy the ARN printed by the command into this operator-session variable.
   $appArn = 'arn:aws:secretsmanager:REGION:ACCOUNT:secret:novamind-crp-postgres-app-dev-EXAMPLE'
   ```

4. Create the dedicated `novamind-crp-bootstrap-execution-dev` role and the
   separate `novamind-rds-bootstrap-task` role from
   `infrastructure/novamind-rds-bootstrap-support.template.json`. Set
   `CreateBootstrapExecutionRole=true`, provide the exact bootstrap ECR
   repository ARN as `BootstrapEcrRepositoryArn`, and use the exported
   `BootstrapExecutionRoleArn` as `ExecutionRoleArn` when registering the task.
   The execution role has only ECR authorization/image-pull and bootstrap log
   delivery permissions. It has no Secrets Manager or RDS permissions. The
   task role has the ECS task trust policy and the rendered bootstrap policy;
   only that separate task role may read the RDS master secret and write the
   application secret. Keep both roles separate from permanent backend roles.

   ```powershell
   $policyPath = Join-Path $env:TEMP 'novamind-rds-bootstrap-policy.json'
   $policy = (Get-Content "$root\infrastructure\novamind-rds-bootstrap-task-role-policy.template.json" -Raw).
     Replace('__RDS_MASTER_SECRET_ARN__', $masterArn).Replace('__APPLICATION_SECRET_ARN__', $appArn)
   [System.IO.File]::WriteAllText($policyPath, $policy, [System.Text.UTF8Encoding]::new($false))
   aws --profile $profile iam put-role-policy --role-name novamind-rds-bootstrap-task `
     --policy-name novamind-rds-bootstrap --policy-document "file://$policyPath" --no-cli-pager
   Remove-Item -LiteralPath $policyPath -Force
   ```

   If either secret uses a customer-managed KMS key, render and attach
   `novamind-rds-bootstrap-custom-kms-policy.template.json` to the bootstrap
   task role. Attach only
   `novamind-rds-backend-execution-custom-kms-policy.template.json` to the
   permanent backend execution role. Do not reuse the bootstrap KMS policy for
   the backend: it includes the master key by design.

5. Create a security group for the bootstrap task with no inbound rules. Permit
   its outbound TCP `5432` only to the RDS security group. The RDS security
   group must allow TCP `5432` from both this bootstrap group and the permanent
   backend task group.

   The default invocation uses private subnets and therefore needs approved
   private egress (NAT or the required VPC endpoints) for ECR, CloudWatch Logs,
   Secrets Manager, and STS. A one-time alternative is an explicitly selected
   public subnet with `-UsePublicBootstrapNetwork`: Fargate assigns the task a
   public IP, the subnet must have an active IGW route, and the task security
   group still has **zero inbound rules**. Restrict outbound to RDS `5432` and
   public HTTPS `443`; do not make RDS public. This exception applies only to
   the bootstrap task, not the permanent private API service. See the
   consolidated deployment runbook for the read-only network checks.

6. Start exactly one bootstrap task. Its helper is plan-only unless `-Apply`
   is supplied. Substitute the existing cluster, private subnets, security
   group, ECR digest, roles, and log group; none of these values is a secret.

   ```powershell
   .\scripts\Start-NovaMindRdsBootstrap.ps1 `
     -MasterSecretArn $masterArn -ApplicationSecretArn $appArn `
     -BackendImageUri 'ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/novamind-backend@sha256:IMAGE_DIGEST' `
     -BootstrapTaskRoleArn 'arn:aws:iam::ACCOUNT:role/novamind-rds-bootstrap-task' `
     -ExecutionRoleArn 'arn:aws:iam::ACCOUNT:role/novamind-ecs-execution' `
     -ClusterName 'novamind' -SubnetId 'subnet-PRIVATE_A','subnet-PRIVATE_B' `
     -BootstrapSecurityGroupId 'sg-BOOTSTRAP' -CloudWatchLogGroup '/ecs/novamind-rds-bootstrap' `
     -ProfileName $profile -Region $region -Apply
   ```

   For the explicitly reviewed public-bootstrap alternative, pass the existing
   public subnet (for example `subnet-0405745b30f4764a0`) and append
   `-UsePublicBootstrapNetwork`. Do not assume the saved historical inventory
   proves that subnet's current route table or network controls.

   A successful task creates or re-hardens `novamind_app`, creates `novamind`
   only if it does not already exist, rejects a schema owned by another role,
   revokes public schema access, runs migrations as the app role, and finally
   writes the runtime secret. It leaves existing database objects untouched if
   schema ownership does not match, rather than taking ownership.

7. Give the permanent backend **execution** role only the rendered
   `novamind-rds-backend-execution-role-policy.template.json` (and its
   application-only custom-KMS policy where needed). Merge
   `novamind-rds-backend-container-fragment.template.json` into the existing
   backend container definition, replacing the application-secret ARN. Do not
   add the master ARN to the backend task definition, task role, execution role,
   environment, or secret list. Preserve the current Cognito and Bedrock task
   permissions unchanged.

8. Register and deploy the updated backend task definition through the normal
   reviewed release process. Verify one replacement task starts successfully,
   then check its health through the existing private ALB path. Do not log the
   task environment, DSN, or secret values. The bootstrap task can remain
   stopped for audit; do not schedule it or attach it to the ECS service.

## Validation and recovery boundaries

- The bootstrap job's only success message is that role, schema, and migrations
  are ready. On failure inspect the tightly scoped CloudWatch log and ECS stop
  reason; neither should require copying a secret into a terminal.
- A failed migration does not write the final app connection secret. Correct the
  cause and rerun the same task with the same app secret; do not create a second
  credential set or bypass migrations.
- If the schema already exists with a different owner, stop. Investigate its
  ownership and data lineage before any manual database action.
- The task does not reset a database, touch SQLite, rotate the RDS master user,
  or weaken immutable audit/HITL triggers. Application migrations retain their
  normal transactional, versioned behavior.
- Before a production change window, exercise this exact image and role setup
  against a separate disposable PostgreSQL database. The Phase C test DSN must
  never target this RDS instance.
