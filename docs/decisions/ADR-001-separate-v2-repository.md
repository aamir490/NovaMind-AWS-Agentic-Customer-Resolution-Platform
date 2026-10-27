# ADR-001: Separate V2 repository

Status: Accepted

## Context

`Chatbot_text_image` is the V1 Streamlit + Bedrock reference prototype. NovaMind V2 has a broader goal: customer cases, agent-driven information gathering, deterministic rules, controlled actions, and real AWS deployment verification. Learning and interview explanation require a clear account of how each layer is introduced.

## Decision

Build V2 in `NovaMind-Agentic-Customer-Resolution-Platform` as a separate repository. Keep V1 as a reference. Do not copy it wholesale.

## Alternatives

- Extend V1 in place: retain existing work, but mix prototype assumptions with the new architecture and learning sequence.
- Copy V1 into a new repository: gain a new location while carrying forward unreviewed code and dependencies.
- Start a separate V2 and selectively review reuse: make each dependency and component a deliberate decision. This is the accepted option.

## Why a separate V2 repo

The intended frontend, API, agent, tool, domain, and persistence boundaries differ from the reference prototype. A separate repository lets us establish those boundaries without implying that V1 components already satisfy V2 requirements.

## Benefits

- Clear project identity and implementation history.
- Smaller increments that can be learned, demonstrated, and reviewed.
- Explicit control over reused code, dependencies, and architecture assumptions.

## Trade-offs

- Some setup and useful behavior may need to be recreated.
- Selective migration requires review effort.
- V1 fixes do not automatically appear in V2, and the two projects must be distinguished in explanations.

## Migration strategy

Review one idea or component at a time. Identify its purpose, assumptions, dependencies, data handling, and verification needs. Decide whether to reuse, adapt, or replace it, then record the source and reasoning in the scoped change. Do not import credentials, local environments, generated artifacts, or unrelated prototype code. No V1 source has been migrated in Phase 0.

## Consequences

V2 must earn its own test and deployment evidence. V1's Bedrock experience is reference material, not proof that V2 has a working model integration. A separate repository is not itself a guarantee of production quality.

## Interview explanation

"I kept the Streamlit/Bedrock prototype as V1 and started NovaMind in a separate repository because the target system needs explicit API, domain, tool, approval, and persistence boundaries. I review reusable components individually so I can explain their assumptions and verify them in V2."
