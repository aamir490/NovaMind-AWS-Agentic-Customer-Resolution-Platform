# NovaMind CRP dev private ECS egress

`infrastructure/novamind-crp-dev-private-egress.template.json` is an additive,
opt-in dev stack for the existing VPC `vpc-0baf042257d729f7d`. It creates two
new ECS private subnets (`172.31.96.0/24` in `us-east-1a` and
`172.31.97.0/24` in `us-east-1b`), one private route table per subnet, one NAT
Gateway in an existing public subnet, and one S3 Gateway Endpoint.

It does not modify the CloudFront VPC Origin subnets. The existing origin CIDRs
`172.31.112.0/24` and `172.31.113.0/24` remain owned by
`novamind-ecs-network-service.template.json` and are not associated with these
route tables or the S3 endpoint.

## Required read-only preflight

Before creating a change set, independently verify and record:

1. The VPC is `vpc-0baf042257d729f7d` and has no overlapping secondary CIDR.
2. `172.31.96.0/24` and `172.31.97.0/24` are unused and do not overlap any
   existing subnet, RDS subnet group, peering, or transit route.
3. The supplied public subnet is in the same VPC, has a route-table association,
   and that route table has `0.0.0.0/0` routed to an Internet Gateway.
4. The two origin subnets remain `172.31.112.0/24` and `172.31.113.0/24` and
   are not selected as the NAT public subnet or ECS private subnets.
5. RDS security-group ingress allows TCP 5432 from the API and 6F workload
   security groups only. This stack does not alter RDS security groups.

CloudFormation cannot prove current VPC CIDR availability or the existing public
route from a parameter alone. The template therefore fails its parameter rules
when the public subnet or route-table sentinel is left in place, and restricts
the VPC, AZs, and reviewed CIDRs to the approved dev values. It also requires
`PublicSubnetInternetGatewayRouteConfirmed=true` and
`PrivateCidrAvailabilityConfirmed=true`; both default to `false` and must only
be set after the inventory evidence exists. A change set must not be created
until the remaining inventory evidence exists.

## Dependency order

1. Complete the read-only preflight and retain the evidence with the change-set
   review.
2. Create and inspect a change set for this stack.
3. After approval, execute the stack and wait for both private route-table
   associations, the NAT Gateway, and the S3 endpoint to become available.
4. Pass the exported `EcsPrivateSubnetIds` to the existing 6G2 services stack.
   Keep `AssignPublicIp` disabled.
5. Verify NAT-based HTTPS access for Cognito JWKS, Secrets Manager, SQS,
   CloudWatch Logs, ECR, and Bedrock. S3 traffic should use the gateway endpoint.
6. Verify RDS connectivity from API and worker security groups.

## Rollback

If validation fails before ECS cutover, remove only this additive stack after
capturing diagnostics. The existing CloudFront Origin stack, ALB, origin
subnets, queues, secrets, and RDS are outside this stack and must not be
changed as rollback actions. Do not delete the retained S3 endpoint or NAT
resources manually outside the reviewed stack change set.

The one-NAT design is intentionally dev-only: it minimizes fixed cost but is a
single-AZ egress dependency. Production should use a reviewed multi-AZ egress
design before adopting this template.
