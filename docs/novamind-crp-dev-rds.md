# NovaMind CRP dev RDS PostgreSQL

`infrastructure/novamind-crp-dev-rds.template.json` is an additive,
CloudFormation-managed dev database stack. It creates only:

- one encrypted, private, single-AZ PostgreSQL instance;
- one DB subnet group using `subnet-0ef827a350fddfba6` and
  `subnet-05963f195c4bdbbe5`;
- one dedicated RDS security group with no ingress rules.

The stack is restricted to VPC `vpc-0baf042257d729f7d`, database
`novamindcrp`, PostgreSQL port 5432, a low-cost `db.t4g.micro` class, and the
minimum 20 GiB gp3 allocation. RDS manages the master password in Secrets
Manager. The master secret is not injected into permanent ECS tasks.

## Fail-closed preflight

CloudFormation parameters cannot prove that the supplied subnet IDs currently
belong to the expected VPC, are private, or occupy distinct Availability Zones.
The stack therefore defaults these confirmations to `false`:

- `SubnetIdentityConfirmed`
- `SubnetAvailabilityZonesConfirmed`
- `DatabaseIdentityConfirmed`

Set them to `true` only after a separate read-only inventory confirms the VPC,
subnets, AZs, route tables, and approved dev retention/deletion posture. The
CloudFront Origin subnets (`172.31.112.0/24` and `172.31.113.0/24`) are not
referenced by this stack.

## Security-group sequencing

This stack deliberately adds no 5432 ingress. After the RDS security-group ID
is known, add separately reviewed permanent ingress rules for:

1. the ECS API security group;
2. the 6F publisher/worker workload security group.

Each rule must use `SourceSecurityGroupId` and TCP 5432. Do not authorize the
VPC CIDR or `0.0.0.0/0`. Temporary bootstrap TCP 5432 is owned only by
`novamind-rds-bootstrap-support.template.json` and must be removed from that
stack after the one-shot schema initialization task succeeds.

## Deployment order

1. Confirm the two subnet identities, private routing, distinct AZs, and no CIDR
   conflict.
2. Create and inspect the RDS stack change set.
3. Wait for the DB instance to become available and record only its endpoint,
   port, RDS security-group ID, DB subnet group name, and RDS-managed master
   secret ARN.
4. Add the separately reviewed API/workload/bootstrap ingress rules.
5. Create the application seed secret containing `novamind_app` and the fixed
   path to the separately supplied reviewed RDS CA bundle using the existing
   helper; do not store secret values or the CA PEM in this repository or
   CloudFormation parameters.
6. Run the existing one-shot bootstrap task from
   `backend/container/rds_bootstrap.py` to create/validate the `novamind` schema,
   role ownership, and migrations through version 13.
7. Validate database identity, schema ownership, migration ledger, and required
   tables before deploying permanent ECS services.
8. Inject only the final application secret into API, publisher, and worker
   execution roles.

## Cost and rollback

The dev posture minimizes fixed cost by using a single-AZ instance, one small
Graviton class, 20 GiB gp3 storage, one-day automated backup retention, and no
RDS Proxy, Multi-AZ standby, Performance Insights, or Enhanced Monitoring.
RDS instance-hours, storage, backup storage beyond the allowance, and snapshot
retention remain billable; exact prices depend on the active us-east-1 rate
card.

The DB instance and subnet group use `Snapshot` deletion/update replacement
policies. A rollback before cutover should remove only the unconsumed stack
change set. If the instance has been initialized, do not delete it as a normal
rollback: preserve the final snapshot, inspect the schema/bootstrap evidence,
and make any recovery decision explicitly. Never roll back migrations or
replace the application secret to bypass a failed bootstrap.
