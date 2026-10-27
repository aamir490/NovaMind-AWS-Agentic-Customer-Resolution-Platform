# Project scripts

`container_smoke.py` supplies read-only loopback verification for an already running Phase 17 Compose stack:

```powershell
./backend/.venv/Scripts/python.exe -B scripts/container_smoke.py --port 8080
```

It checks frontend assets/health, proxied backend health, anonymous API denial, trace headers, and missing-asset routing. It prints boolean results and exits nonzero if a check fails. No containers are started, no credentials are supplied, no redirects/proxies are followed, and no remote provider is called. Running-container verification remains pending because the Docker engine was unavailable during implementation. See [Phase 17 commands and limits](../docs/phase-17-containers.md).

Existing non-container commands remain in [local development](../docs/local-development.md). This utility performs no AWS deployment or infrastructure provisioning.

AWS release helpers are intentionally plan-only until `-Apply` is supplied.
`New-NovaMindCloudFormationChangeSet.ps1` creates a reviewable change set but
does not execute it; the RDS, ECS, Cognito, CloudFront, frontend, and release
verification helpers are ordered in [the AWS runbook](../docs/aws-deployment-execution-runbook.md).
For the domain-free development path, the CloudFront helper accepts only a
deployed VPC Origin and private internal-ALB DNS name; it will not make a
public ALB origin. ECS must receive an explicit `PublicTaskEgress`,
`ExistingNat`, or `ApprovedEgressProxy` selection.
