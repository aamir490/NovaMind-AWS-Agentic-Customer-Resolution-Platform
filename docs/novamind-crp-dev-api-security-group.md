# NovaMind CRP dev API security group

`infrastructure/novamind-crp-dev-api-security-group.template.json` is an
additive, opt-in security-group stack. It creates only the API security group
in `vpc-0baf042257d729f7d`.

The group has no ingress rules. Its egress is limited to:

- TCP 5432 to the RDS security group imported from
  `${ProjectPrefix}-${Environment}-RdsSecurityGroupId`;
- UDP 53 to `172.31.0.0/16`;
- TCP 53 to `172.31.0.0/16`;
- TCP 443 to `0.0.0.0/0`, routed through the existing private-subnet NAT
  Gateway for Cognito JWKS and AWS API access such as Secrets Manager.

The private ECS subnets must have an active NAT-backed default route. A
Secrets Manager interface endpoint is not required for this dev design because
the existing NAT path is already shared by SQS, Bedrock, and other AWS APIs.

ALB-to-API ingress is intentionally a separate change. This stack does not
create RDS ingress, ALB resources, ECS services, secrets, or legacy resources.

## Fail-closed deployment

All creation and identity confirmations default to `false`:

- `CreateApiSecurityGroup`;
- `VpcIdentityConfirmed`;
- `RdsExportConfirmed`;
- `DnsCidrConfirmed`.

Set them to `true` only after confirming the VPC, RDS export, API subnet
placement, and DNS CIDR scope. The RDS stack must already export
`novamind-crp-dev-RdsSecurityGroupId`.

## Dependency and rollback order

1. Deploy/verify the RDS stack and its exported RDS security-group ID.
2. Create this API security-group stack with all explicit confirmations.
3. Add separately reviewed ALB-to-API ingress.
4. Attach the exported API SG to the private ECS API service.
5. Validate PostgreSQL TLS connectivity and VPC DNS resolution.

For rollback, remove only the ALB ingress first, then stop or detach the API
service before deleting this security-group stack. Do not remove the RDS SG or
its approved ingress rules as part of this stack's rollback. No database,
master secret, application secret, or protected legacy secret is owned here.
