# NovaMind CRP dev RDS ingress

`infrastructure/novamind-crp-dev-rds-ingress.template.json` is an additive
ingress-only stack. It creates exactly two permanent TCP 5432 security-group
ingress rules on the RDS security group:

1. API security group, supplied as a typed parameter because the existing
   network stack exposes `ApiSecurityGroupId` as an output but not an export.
2. 6F workload security group, imported from
   `${ProjectPrefix}-${Environment}-WorkloadSecurityGroupId`.

Temporary bootstrap TCP 5432 ingress is deliberately not part of this stack.
`infrastructure/novamind-rds-bootstrap-support.template.json` is its sole
owner, guarded by `CreateBootstrapSecurityGroup` and the explicit
`BootstrapPublicInternetEgressApproved` approval. This prevents two stacks
from attempting to create the same rule.

The RDS security group is imported from
`${ProjectPrefix}-${Environment}-RdsSecurityGroupId`, exported by the additive
RDS stack. No VPC-CIDR, public, prefix-list, or `0.0.0.0/0` ingress is created.
The template creates no database, secret, role, or bootstrap task and never
references the protected legacy secrets.

## Dependency order

1. Create the RDS stack and wait for the private PostgreSQL instance to become
   available.
2. Verify the RDS stack export and the API/bootstrap SG IDs. Verify all source
   groups belong to the same VPC.
3. Create this ingress stack with `IngressSourcesConfirmed=true`; this creates
   only the permanent API/workload rules.
4. Separately create the bootstrap-support stack with its explicit bootstrap
   approvals before running the one-shot bootstrap task using the existing
   master-secret
   isolation flow.
5. Verify database `novamindcrp`, schema `novamind`, role/schema ownership by
   `novamind_app`, and migration ledger version 13.
6. Deploy/validate the permanent API and 6F publisher/worker paths.
7. Remove the bootstrap-support stack after migration verification, then retain
   this permanent ingress stack for API and workload access.

Do not enable the historical bootstrap ingress resource in
`novamind-rds-bootstrap-network.template.json` alongside
`novamind-rds-bootstrap-support.template.json`. The support stack is the
canonical temporary-ingress owner; use only one reviewed bootstrap-ingress
owner.

## Rollback

Before bootstrap starts, delete or abandon the unexecuted change set. After the
RDS instance has been initialized, do not delete the database as a routine
rollback. Remove the bootstrap-support stack only after confirming the
bootstrap task is stopped and no migration task still needs database access.
If the bootstrap task fails, retain temporary access while investigating the
failure, then remove the bootstrap rule after recovery. Never add a broad CIDR
rule or expose the master secret to permanent ECS tasks.

The RDS instance, master secret, application secret, migrations, API SG,
workload SG, and bootstrap task are owned by separate stacks/processes and are
outside this template's rollback scope.
