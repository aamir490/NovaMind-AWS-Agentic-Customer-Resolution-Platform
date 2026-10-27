# AWS infrastructure templates

The templates here are deliberately opt-in and reference existing NovaMind
resources by parameter. They do not import, replace, or delete existing RDS,
ECS, ECR, S3, CloudFront, VPC, NAT, or legacy secrets.

- `novamind-crp-dev-ecr.template.json` can create the single private
  `novamind-crp-backend-dev` ECR repository. Creation defaults to `false`;
  when enabled it uses AES-256 encryption, scan-on-push, immutable tags, a
  50-image lifecycle cap, and required project/environment/management tags.
- `novamind-crp-dev-ecs-cluster.template.json` can create the
  `novamind-crp-ecs-cluster-dev` Fargate cluster with CloudWatch container
  insights enabled. Creation defaults to `false` and it creates no services or
  task definitions.

- `novamind-cognito-components.template.json` can create only confirmed-missing
  Cognito pool/client/groups; every creation switch defaults to `false`.
- `novamind-ecs-network-service.template.json` can add confirmed-missing
  dedicated private origin subnets, an internal HTTP ALB, CloudFront VPC
  Origin, ECS security groups, target group, listener, or service. It never
  creates NAT, endpoints, DNS records, certificates, RDS, or a public
  CloudFront API origin. Public-task egress is an explicit, security-group
  constrained development option, not an inbound API path.
- `novamind-rds-bootstrap-support.template.json` can add the one-shot bootstrap
  role/log group, an opt-in dedicated zero-ingress bootstrap security group,
  and narrowly attach application-secret/Bedrock policies to existing roles.
  The bootstrap group permits only RDS PostgreSQL plus explicitly approved
  TCP/443 public HTTPS for the one temporary public-IP task; it cannot expose
  an inbound service. The API policy cannot read the master secret.
- `novamind-rds-bootstrap-network.template.json` is the isolated, secret-free
  change set for the dedicated bootstrap security group and the additive RDS
  PostgreSQL ingress rule. It is the only template used for the network fix.

Use `scripts/New-NovaMindCloudFormationChangeSet.ps1` to create a change set,
inspect it, and execute it only after approval. See the concise ordered
deployment runbook in `docs/aws-deployment-execution-runbook.md`.

## Task 6F investigation messaging foundation

`novamind-crp-investigation-messaging.template.json` is the dev-only,
CloudFormation-managed foundation for the transactional outbox path. It adds
an encrypted investigation SQS queue, a retained encrypted DLQ, explicit
redrive/retention settings, separate API/publisher/Strands-worker ECS task
roles, workload security-group egress, and three retained CloudWatch log
groups. Every resource is tagged `Project=novamind-crp`, `Environment=dev`,
and `ManagedBy=CloudFormation`.

The task roles do not read Secrets Manager directly. Later task definitions
must inject only the application PostgreSQL secret through a separately
scoped ECS execution role; this foundation contains no secret ARN and no
legacy secret reference.

The queue visibility timeout defaults to 300 seconds. The current bounded
Strands model timeout is 30 seconds and the required shutdown margin is 60
seconds, so the template enforces a minimum of 90 seconds. The 300-second
default is deliberately above that bound; any future increase in bounded
execution must raise the queue parameter before deployment.

The template intentionally does not create ECS task definitions or services.
Those resources are added by the separate Task 6G2 service template after the
6G1 entrypoints are available. No legacy `novamind/groq-api-key` or
`novamind/auth-credentials` secret is referenced.

## Task 6G2 ECS services

`novamind-crp-investigation-services.template.json` references the 6F queue,
security-group, task-role, and log-group outputs and creates three independent
Fargate task definitions and services: API, transactional-outbox publisher,
and Strands/SQS worker. All use the 6G1 `backend.container.ecs_entrypoint`
with explicit `NOVAMIND_PROCESS_ROLE` values. The API has no inline executor or
Bedrock task permission; only the worker task role receives the approved
Bedrock permissions from 6F.

The 6F stack exports the queue URL, task-role ARNs, workload security-group ID,
and log-group names under the `novamind-crp-dev-*` export namespace. 6G2 uses
`Fn::ImportValue` for those resources rather than accepting independently
typed queue/role/log parameters. The worker is PostgreSQL/Strands-only and
auth-free; only the API receives Cognito configuration secrets.

The service template creates separate ECS execution roles scoped to the
project-specific PostgreSQL and Cognito configuration secrets and existing log
groups. Secret parameters require the `novamind-crp-` namespace and do not
accept legacy secret names. All services use private subnets, disabled public
IPs, read-only roots, bounded 120-second shutdown, and deployment circuit
breakers. The API alone has an HTTP health check and ALB target; publisher and
worker remain queue/database processes without an HTTP listener.
