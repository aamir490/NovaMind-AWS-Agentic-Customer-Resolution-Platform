# Phase 12 — Conversation Memory & Persistence

**Phase 12 complete**, following independent verification reported by the user. Built on the independently verified Phase 11 baseline. Its verification and implementation-time limits are recorded below. Phase 13 subsequently adds local authentication and authorization; see [the current authenticated calling contracts](phase-13-security.md). Phase 14 has NOT started.

**Historical scope note:** This guide records Phase 12. Programmatic memory/agent/HITL examples now require the Phase 13 `authenticated(provider, token)` scope. Phase 13 replaces the unauthenticated reviewer limitation with a server-derived reviewer UUID and rejects supplied reviewer names. Storage, restart/recovery limits, untrusted-memory treatment, and the separate business/checkpoint/audit responsibilities remain unchanged.

## Four separate responsibilities

- **Conversation memory:** durable local SQLite conversation metadata and user/assistant text only.
- **LangGraph/HITL state:** existing per-run state and Phase 10 `InMemorySaver`; checkpoints still disappear on restart.
- **Business/domain state:** existing customer, order, case, inventory, policy, and proposal services remain authoritative. Their storage has not moved into SQLite.
- **Audit history:** existing structured agent/tool and human-review events remain separate and process-local. Conversation text is not an audit log.

No memory tool, HTTP endpoint, frontend UI, authentication, cloud infrastructure, or new business action was added. Memory is explicitly injected into the existing Python agent/workflow interfaces; the HTTP application's startup does not create a database.

## Validated contracts and interface

`backend/app/conversations.py` defines frozen Pydantic contracts with unknown fields forbidden and instance revalidation enabled:

- `Conversation`: server-generated UUID `conversation_id`, bound `case_id`, UTC `created_at`, and strict integer `message_count` (0–10000).
- `MessageInput`: role exactly `user` or `assistant`; trimmed, nonblank content of 1–8000 characters. System/tool roles and authority/reviewer/status fields are invalid.
- `ConversationMessage`: message input plus conversation UUID, server-managed integer sequence (1–10000), and server UTC timestamp. Sequence, rather than wall-clock order, determines ordering.
- `ConversationHistory`: metadata and ordered messages; validates complete contiguous sequences starting at 1, matching conversation IDs, and exact message count.
- `HistoryLimits`: strict `max_messages` (1–100, default 20) and `max_chars` (512–64000, default 16000).

`ConversationStore` is the replaceable protocol: `create(case_id)`, `load(conversation_id, case_id)`, `list(case_id)`, and `append(conversation_id, case_id, MessageInput)`. Creation returns metadata, loading returns validated full history, listing returns case-scoped metadata, and append returns the stored message. `ConversationService` checks live case existence, verifies adapter output and bindings, and constructs bounded context. No storage adapter can extend the model tool catalog.

## SQLite storage

The implementation uses Python's standard-library `sqlite3`; dependencies are unchanged. The configured filesystem path is application configuration, never a tool/model argument. Schema version is `PRAGMA user_version=1`:

```sql
conversations(conversation_id TEXT PRIMARY KEY, case_id TEXT,
              created_at TEXT, message_count INTEGER)
messages(conversation_id TEXT REFERENCES conversations(conversation_id),
         sequence INTEGER, role TEXT, content TEXT, created_at TEXT,
         PRIMARY KEY(conversation_id, sequence))
```

All fields are non-null. SQL checks constrain count/sequence, roles, and content length; Pydantic additionally validates IDs, UTC timestamps, whitespace, complete history, and bindings. An index covers `(case_id, created_at, conversation_id)` for deterministic listing. The case ID is a logical binding, not a foreign key into business tables; there are no business tables in this database.

Connections are short-lived, enable foreign keys, and use a five-second busy timeout. `BEGIN IMMEDIATE` serializes append sequence allocation across store instances. Inserting the message and updating its count commit together or roll back together. Read transactions keep metadata/history coherent. Full history validation detects gaps and inconsistent counts before reads or appends. Startup checks database integrity, foreign keys, schema version, and required columns. SQL uses bound parameters.

The constructor opens an existing file by default. `initialize=True` explicitly creates a new file and schema only if that file is absent; existing invalid/empty files are rejected, never overwritten or migrated. Normal operations use SQLite `mode=rw` so a file deleted after startup is not silently recreated. Unknown versions fail closed; schema migration is not implemented.

## Local usage and case binding

```python
from pathlib import Path
from backend.app.agent import AgentRequest
from backend.app.conversations import ConversationService, HistoryLimits, SQLiteConversationStore
from backend.app.graph_agent import GraphResolutionAgent

# Provision once in an application-selected directory whose parent already exists:
path = Path("local-conversations.sqlite3")
store = SQLiteConversationStore(path, initialize=True)
# On subsequent service/process startup: SQLiteConversationStore(path)
memory = ConversationService(store, app.state.case_service, HistoryLimits())
conversation = memory.create(case_id)  # Requires an existing authoritative case.
agent = GraphResolutionAgent(llm, app.state.local_tools, memory=memory)
result = agent.run(AgentRequest(
    case_id=case_id, conversation_id=conversation.conversation_id,
    message="Please review the information I supplied earlier.",
))
history = memory.load(conversation.conversation_id, case_id)
case_conversations = memory.list(case_id)
```

The same `memory=` parameter works with `ResolutionAgent` and `HITLWorkflow`. Supplying memory without a conversation ID creates a fresh case-bound conversation; results return its ID for future requests. With no memory and no conversation ID, prior behavior stays stateless. Supplying a conversation ID without configured storage fails safely.

Case existence is checked before memory access; store load/append require both IDs and reject mismatches. Lists are scoped to one case. Neither missing conversation IDs nor wrong bindings create replacement conversations. A UUID provides correlation, not authentication or tenant authorization. Direct Python/store/filesystem access is a trusted local administrative capability.

## Agent/LangGraph context and limits

After authoritative case preflight, the agent loads history, constructs the bounded envelope, and appends the current user message before any LLM call. Prior roles/text/sequence/timestamps are serialized as data in one `user`-role JSON envelope labelled **UNTRUSTED CONTEXT**. Stored assistant text is not replayed as a provider assistant decision; stored text never becomes a system message. The current request and fresh case tool result remain separate. System instructions explicitly prohibit using memory as authority.

History selection walks backward through at most the latest 20 messages by default, retaining a contiguous suffix in chronological sequence order. The entire JSON envelope, including metadata and escaped Unicode, must fit 16000 characters by default. If the next whole message does not fit, selection stops; no partial messages, generated summaries, or substitution of older messages occur. The current user message appears once in current-request context, not again in the history envelope. Durable text is not truncated. These are character/count limits, not tokenizer limits. Existing LLM step and output-token budgets independently bound the current run.

The Phase 8 loop and Phase 9 graph persist their application-rendered customer response at termination. Tool calls, tool results, reasoning decisions, pending-proposal maps, reviewer identity, and audit events are not saved as conversation records. HITL saves the application-rendered pause/completion/review response. A valid human resume can append another assistant response without a user message: the decision itself stays in the existing separate review/audit contracts. Invalid/replayed resumes append nothing and never invoke the LLM.

## Restart, recovery, and failures

Reopening the same valid database preserves IDs, text, timestamps, and sequence numbers. Recreating the conversation service and agent against the same authoritative case service permits continued conversation with fresh per-run state. SQLite transactions protect individual appends and counter updates, including concurrent appends from independent instances.

**A complete current application restart still loses business cases and HITL checkpoints.** The raw store preserves the conversation, but the conversation service refuses access if its case no longer exists. This phase deliberately does not restore missing cases from memory or reconstruct workflow/reviewer state. Continuing after a full process restart requires the same case IDs to be available from an authoritative domain source; implementing durable domain storage is outside this phase. A recreated HITL service rejects old workflow IDs as `UNKNOWN_WORKFLOW`.

Missing/corrupt/invalid data and adapter failures stop reasoning safely with stable errors such as `CONVERSATION_NOT_FOUND`, `CONVERSATION_CASE_MISMATCH`, or `CONVERSATION_UNAVAILABLE`. No raw storage paths/errors enter agent responses. A failed initial user append causes no LLM invocation. Invalid request contracts are rejected by Pydantic.

If saving the final response fails, agents report `CONVERSATION_WRITE_FAILED` while retaining actual tool outcomes and pending proposal IDs. HITL retains the actual `REVIEW_REQUIRED` or `REVIEWED` status and IDs and reports the memory-write error separately; the human pause remains resumable, and a completed review cannot replay. Clients must inspect both status and error. A conversation-write error never retries a business operation or changes a human decision.

Conversation writes, business operations, and checkpoints are separate transactions. A crash may leave a user message without an assistant response, an empty newly created conversation, or a business/review outcome without saved response text. Unexpected graph/runtime failures can likewise leave incomplete conversational turns. No automatic repair, replay, exactly-once turn processing, or cross-store transaction is claimed.

## Trust and retained safety boundaries

Stored messages are **untrusted context**, including text previously produced by the application. They cannot register tools, modify eligibility rules, approve/reject proposals, supply reviewer identity, execute actions, change graph routing, or overwrite business state. Enforcement remains the existing application tool allowlist, strict tool inputs, deterministic rules, case-bound proposal guard, one-proposal/step limits, separate live proposal lookup, and explicit human resume contract. RAG evidence remains untrusted reference information.

Prompt injection can still influence which permitted tool requests or proposals a model suggests; prompts do not guarantee truthful reasoning or verified scenario facts. Human identity remains an unauthenticated caller-supplied label. SQLite is not encrypted, authenticated, tamper-proof, or an authorization system. Structurally valid edits by someone with filesystem access cannot be detected as forged text; even valid text remains untrusted.

## Verification and limitations

### Independent verification reported by the user

- Phase 12 focused conversation/persistence tests: **25/25 passed**.
- Full backend regression: **169/169 passed**.
- Frontend API tests: **6/6 passed**.
- Frontend production build: **passed**.
- `git diff --check`: **passed with CRLF/LF normalization warnings only**.

These results were recorded without rerunning tests or builds during this documentation-only update. They do not establish live model, network-service, or browser verification. Persistence architecture, business rules, RAG, HITL, and safety boundaries remain unchanged.

### Implementation-time focused evidence and preserved limitations

```powershell
./backend/.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_conversations.py -v
```

**25/25 passed**, using temporary isolated SQLite files and scripted providers only. Coverage includes contract tampering, limits, durable recreation, concurrent ordering, atomic rollback, missing/corrupt/deleted databases, invalid rows, case isolation, bounded serialized context, fresh run state, absent business records, replacement adapters, malicious memory/tool denial, deterministic eligibility, forbidden proposal authority fields, RAG/HITL pause/resume and reviewer separation, checkpoint loss, write failures without replay, step limits, and stateless Phase 8/9 parity.

During implementation, only the focused Phase 12 tests ran; no live Gemini/network/AWS calls, frontend checks, package installations, or broad test suites ran. Subsequent independent results are recorded above, including the **169/169** backend regression and frontend verification. Phase 11's reported 144-test full regression remains historical baseline evidence.

SQLite persists conversation memory only. Business/domain records, LangGraph/HITL checkpoints, and audit history remain in memory. Full application restart cannot resume a conversation until authoritative case state exists again. There are no cross-store transactions or automatic replay, and no authentication, encryption, or frontend memory UI. Stored conversation text remains untrusted context, not authoritative business state.

This is local bounded demonstration storage: at most 10000 messages per conversation, 8000 characters per message, full-history validation on loads/appends, and unpaginated case listing. Concurrent turns may interleave their committed messages; a whole agent turn is not serialized or idempotent. There is no retention/deletion UI, automated backup, encryption, schema migration, shared/distributed deployment support, or domain/checkpoint/audit durability. No database package was added. Phase 12 added no AWS, DynamoDB, RDS, MongoDB, Redis, vector database, authentication, or frontend memory UI. Phase 13's local authentication is documented separately; **Phase 14 has NOT started.**
