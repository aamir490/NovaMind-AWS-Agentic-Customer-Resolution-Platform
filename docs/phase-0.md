# V2 Phase 0A / 0B / 0C scope

## Objective

Establish an understandable, independent foundation for NovaMind without importing the V1 prototype or implementing the agent.

## Scope boundaries

Phase 0A refers to architecture/planning. The minimal React/Vite + FastAPI scaffold was implemented before explicit approval during Phase 0. A read-only audit identified that scope overreach; the user later reviewed and explicitly accepted the scaffold as Phase 0B. Earlier assistant-written text presenting the scaffold as approved Phase 0 scope was not authorization.

Phase 0C normalizes the repository, learning material, architecture documents, and decision records. The accepted local scaffold and existing dependency definitions may remain. Verify with existing dependencies and local loopback only; if the environment is missing or broken, stop rather than install or recreate it. Do not create the initial commit in this phase.

Phase 0C adds no customer case handling, order lookup, policy evaluation, document/image analysis, agent, business tools, actions, or notifications. It adds no packages or AWS integration. See [project evolution](../PROJECT-EVOLUTION.md) for the milestone history and [ADR-002](decisions/ADR-002-react-fastapi-local-foundation.md) for the acceptance decision.

Do not provision AWS resources or claim a deployment has passed verification in this phase. No AWS credentials or model access are required for the local foundation.

## Future implementation sequence

This sequence is a proposed learning path; later phase requirements will define exact scope.

1. Define domain vocabulary, case lifecycle, and deterministic business rules.
2. Implement application services and read-only business tools with controlled sample data.
3. Add one Resolution Agent using the reviewed Strands/Bedrock integration.
4. Add evidence ingestion and multimodal analysis with explicit trust boundaries.
5. Add proposals, authorization, human approval, execution, and verification.
6. Add reviewed AWS persistence and deployment infrastructure.
7. Verify deployment, failure handling, observability, and the end-to-end customer scenario in the real AWS account.

## Completion evidence

Keep the repository independent from V1. Document what exists, what is planned, how to run any included code, and which checks actually passed. Do not present placeholder folders or diagrams as working capabilities.
