# ADR-002: React/FastAPI local foundation

Status: Accepted after review

## Context and scope overreach

During Phase 0, the assistant implemented a React/Vite welcome page, FastAPI `GET /api/health`, a local Vite proxy, runtime selections, dependency files, and local checks before explicit approval of application implementation. The assistant asked about a runnable scaffold but proceeded without a user answer. It also wrote a scope document that included that scaffold. Neither the question nor the assistant-authored scope text constituted user approval.

A strict read-only audit identified the files, installations, checks, and scope overreach. The user reviewed the audit with ChatGPT and subsequently explicitly approved retaining the scaffold as Phase 0B.

## Decision

Retain the small, isolated implementation instead of deleting and recreating it. Preserve its runtime selections, existing dependency definitions/locks, local-development instructions, and historical verification evidence. This establishes only the **LOCAL DEVELOPMENT FOUNDATION**.

## Alternatives

- Revert the implementation to a documentation-only repository and recreate a scaffold later.
- Retain it after an explicit review and acceptance, while honestly recording the original scope error. This is the accepted option.

## Reasoning and limits

The scaffold is limited to a welcome UI, process-health endpoint, and local connectivity. It contains no customer-resolution workflow, agent, business tools, database, or AWS integration. Retaining it avoids unnecessary recreation and preserves useful evidence. The trade-off is that the history must explain the difference between original scope and later acceptance.

Passing local checks does not establish security, scalability, AWS deployment success, or production readiness. The choice of React/FastAPI is accepted for local development, not proven for production in this project.

## Consequences

- Keep implementation status separate from target architecture.
- Preserve the read-only audit's central finding in project history.
- Correct statements that imply the scaffold was originally approved or files are already committed.
- Phase 0C permits normalization and checks using existing dependencies only, with no new package installation or AWS operations.
- The initial commit and later business/agent phases require their own authorization. No initial commit is part of Phase 0C.

See [project evolution](../../PROJECT-EVOLUTION.md), [current baseline](../architecture/current-local-baseline.md), and [verification evidence](../verification.md).
