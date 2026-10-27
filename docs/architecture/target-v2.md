# Target V2 architecture

Status: **TARGET / NOT YET IMPLEMENTED**. This is the future customer-resolution architecture, not the current local scaffold.

```text
React
    -> FastAPI
    -> Application services
    -> Resolution Agent
    -> Strands
    -> Amazon Bedrock
    -> approved business tools
    -> domain rules
    -> repositories/providers
    -> AWS persistence
```

This is a conceptual responsibility flow. Strands would coordinate model calls and tool execution in the backend; Bedrock would not directly receive database credentials or bypass application authorization. Repositories/providers would adapt domain operations to storage and external services.

## Why these boundaries exist

The React/Vite frontend presents case information and collects user input. It must not contain AWS credentials, make authorization decisions, or invoke sensitive business actions directly.

FastAPI is the HTTP boundary. Future request validation and authenticated identity enter here. Application services coordinate case operations, authorization, tool access, and persistence. This keeps workflow decisions testable without requiring an LLM call.

One Resolution Agent will interpret the customer's goal and choose among approved tools. Strands Agents SDK is the preferred framework; Amazon Bedrock will provide the model. Begin with one agent so its decisions and failures are easier to understand and trace. Additional agents require a demonstrated need and a reviewed design.

The approved tool layer exposes narrow operations with validated inputs and outputs. Tool availability alone does not grant permission to act on any order or case. Deterministic domain rules determine eligibility independently of model wording. Future repository/provider adapters will use DynamoDB for structured records and S3 for uploaded evidence; schemas, retention, and access controls are not yet designed.

Cognito identity, ECS container deployment, and CloudFront delivery are planned capabilities. Their exact configuration and suitability remain subject to future design and deployment verification. Evaluations, observability, and CI/CD are also future work. Nothing in this document establishes production readiness.

## Planned tool progression

Start with read-only business tools:

- `get_order()`
- `get_policy()`
- `check_inventory()`

Later capabilities:

- Evidence: `analyze_document()`, `analyze_image()`.
- Resolution: `evaluate_resolution()`, `propose_resolution()`.
- Actions: `create_replacement()`, `create_refund()`.
- Verification and communication: `get_action_status()`, `send_notification()`.

These names are a roadmap, not executable interfaces or finalized signatures.

## Sensitive action lifecycle

1. **PROPOSE:** record the proposed action, supporting evidence, and applicable policy.
2. **AUTHORIZE:** application logic checks the authenticated actor, permissions, policy, and required human approval. Bind the approval to a specific case, action, and parameters.
3. **EXECUTE:** an approved tool rechecks authorization and current state before performing the action. Design for retries without duplicate refunds or replacements.
4. **VERIFY:** inspect the authoritative action status before marking the case resolved or telling the customer the action succeeded. Handle pending, failed, or uncertain outcomes explicitly.

The LLM may propose an action; neither its text nor a tool invocation authorizes that action. Uploaded documents, images, and customer messages are untrusted evidence, not instructions that can override application rules.

## Decisions deliberately deferred

- Bedrock model selection, region availability, access, and cost controls.
- Cognito configuration, actor roles, case ownership, and approval interface.
- DynamoDB access patterns and S3 evidence lifecycle.
- ECS/CloudFront topology, infrastructure-as-code framework, and delivery pipeline.
- Observability, evaluation datasets, failure recovery, and deployment acceptance criteria.

Each future decision should explain the problem, choice, alternatives, tradeoffs, and verification evidence. Production-oriented intent is not a claim of production readiness.

See [the current local baseline](current-local-baseline.md) for what actually exists and [project evolution](../../PROJECT-EVOLUTION.md) for the staged roadmap.
