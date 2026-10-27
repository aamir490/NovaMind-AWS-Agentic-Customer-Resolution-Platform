# Phase 11 — Local knowledge retrieval

**Phase 11 complete**, following independent verification reported by the user. Phase 12 subsequently added local conversation persistence; see [its verification and limitations](phase-12-conversations.md). Phase 13 now requires authenticated context for tool/agent access; see [the local security boundary](phase-13-security.md). Retrieval and its untrusted-evidence treatment are unchanged. Phase 14 has NOT started.

## Corpus and contracts

`backend/knowledge/support.json` contains three curated demonstration documents: wrong-item/damaged-delivery intake, return/refund policy interpretation guidance, and proposal/human-review SOP. These are unstructured reference text, not real merchant policies. They intentionally contain no numerical eligibility windows or entitlements. Existing structured policy, eligibility, inventory, and proposal services remain authoritative and unchanged.

`backend/app/knowledge.py` defines immutable, validated contracts with unknown fields forbidden:

- `SourceMetadata`: stable document ID, title, version, local source URI, and fixed `authority="reference_only"`.
- `KnowledgeDocument`: metadata plus nonblank text bounded to 16000 characters.
- `KnowledgeChunk`: source metadata, SHA-256 chunk ID, text of at most 600 characters, validated start/end character offsets, and fixed `trust="untrusted_information"`.
- `KnowledgeQuery`: trimmed query of 1–500 characters containing a searchable term; strict integer `top_k` of 1–5, default 3.
- `RetrievalHit`: chunk and finite positive similarity score no greater than 1.
- `RetrievalResult`: echoed validated query, bounded unique hits, method identifier, and fixed trust/usage labels.

## Loading, chunking, and local retrieval

The loader reads only its configured local JSON file. The default path is module-relative, independent of the working directory. Tool inputs cannot select a file, URI, directory, or loader. File reads are limited to 256 KiB; corpora must contain 1–16 documents with unique document IDs and source URIs. Invalid or missing corpora produce a safe error.

Documents are sorted by ID. Text is stripped and line endings normalized before chunking. Chunks are deterministic, nonoverlapping 600-character slices; whitespace-only slices are omitted. Boundaries can split words or sentences. Offsets refer to normalized document text, not original file bytes. Chunk IDs hash source metadata, starting offset, and chunk text; identical inputs give identical IDs. No generated summaries or LLM processing occurs during loading/chunking.

`EmbeddingProvider` supplies sparse token-weight vectors; `KnowledgeRetriever` supplies validated query results. Both can be injected for tests or future adapters. The default `TokenEmbedding` uses case-folded Unicode word counts normalized to unit length. `LocalKnowledgeRetriever` scores cosine overlap between query and chunk text plus source title, omits zero-score matches, and breaks ties by document ID and offset. The method is labelled `local_token_cosine_v1`.

This is a lexical baseline, not a neural embedding model or semantic vector database. It loads and indexes the small corpus on each search; there is no retained index, external embedding call, persistent memory, or network access. Replacing the method in future requires maintaining or explicitly extending the result contract; do not silently label a different method as token cosine.

## Controlled tool and LangGraph path

Phase 5 `LocalTools` now has nine tools. The only new one is read-only:

```python
result = app.state.local_tools.invoke("search_knowledge", {
    "query": "wrong item invoice photo", "top_k": 3,
})
```

`search_knowledge` returns the standard `ToolSuccess` envelope with a `RetrievalResult`. Malformed input returns `INVALID_INPUT` before retrieval. Missing/empty/malformed knowledge, embedding failures, retrieval exceptions, or invalid output return `KNOWLEDGE_UNAVAILABLE` without raw exception text or filesystem paths. A valid search with no matching chunks succeeds with empty hits; it does not fabricate evidence. Unknown output fields, mismatched echoed queries, duplicate chunks, and results above `top_k` are rejected.

The existing Phase 8/9 tool catalog discovery automatically exposes this capability and its input/output schemas. The graph passes its result through the same structured tool-result message and audit history as other tools. Phase 10 retains that history through checkpoint and resume. No new graph node, provider code, HTTP endpoint, frontend UI, or business rule is needed. The eight prior tool names/contracts remain available. The older exact-allowlist test expectation is updated for the ninth tool; only Phase 11 tests ran during implementation. Subsequent independent verification is recorded below.

## Sources, evidence, and trust

Every hit carries title, document ID, version, local URI, chunk ID, exact text, and offsets. For example, `local-knowledge://wrong-item-intake` identifies a curated document in `support.json`; it is a source identifier, not a network URL or implemented web route. These fields support citations and audit lookup. Scores measure lexical overlap, not confidence, eligibility, truth, or authorization. The current fixed customer-response templates remain unchanged; citation-ready evidence is exposed in structured tool/audit results rather than a new prose answer UI.

The tool description and output explicitly label retrieved content **UNTRUSTED INFORMATION**. Existing system instructions already treat tool-record text as untrusted. These prompts do not replace enforcement: returned text is data only, tool names are checked against the application allowlist, inputs are validated, and business decisions stay in deterministic services. Retrieval cannot register tools, execute functions, write state, approve/reject, provide reviewer identity, or change graph routing. Human decisions still enter only through the separate HITL resume contract, and review executes no business action.

Prompt injection can still influence what an LLM asks or proposes within the permitted interface. This implementation does not guarantee truthful model reasoning or fact provenance. Scenario inputs remain unverified; a proposal may record an inaccurate suggestion for human review. Local corpus curation and citation labels do not make retrieved text authoritative.

## Focused verification and limits

### Independent verification reported by the user

- Phase 11 focused Knowledge/RAG tests: **15/15 passed**.
- Full backend regression: **144/144 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were recorded without rerunning tests or builds during this documentation-only update. They do not establish live model or browser verification.

### Implementation-time focused evidence and preserved limitations

`./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_knowledge.py -v`

**15/15 passed**: corpus validation/order, normalized chunk spans and IDs, deterministic vectors/ranking/ties, query/top-k bounds, missing/empty/invalid corpora, embedding/output errors, metadata trust contracts, read-only behavior, Phase 8/9 evidence parity, malicious-tool denial, deterministic eligibility preservation, HITL pause/resume with retained citations, and graph termination on retrieval failure.

During implementation, no live Gemini/embedding/network calls, full regression, frontend build, or browser checks ran; subsequent independent results are recorded above. Retrieval remains local lexical/token-cosine only, with no production semantic embedding provider/vector database, live embedding/network calls, or frontend RAG UI. Retrieved content remains untrusted information; deterministic business rules and HITL remain authoritative. No dependencies were added or installed. No Qdrant, OpenSearch, Pinecone, Bedrock Knowledge Bases, S3, production persistence, or authentication exists. Phase 10 limitations remain: reviewer identity is unauthenticated, checkpoints are lost on restart, no durable workflow recovery exists, and approval/rejection executes no actions. Phase 12 adds local conversation text durability only; it does not remove those workflow limitations.
