"""Explicit local LangGraph orchestration; Phase 8 remains the reference implementation."""

import json
from uuid import UUID

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langsmith import tracing_context
from pydantic import Field

from .agent import AgentConfig, AgentDecision, AgentRequest, AgentResult, AuditEntry, Contract, Finish, ToolCall, agent_instructions
from .llm import LLMMessage, LLMRequest, StructuredLLM
from .tools import LocalTools
from .conversations import (ConversationService, ConversationError,
                            begin_memory, finish_memory)
from .security import SecurityError
from .guardrails import GuardrailError, check_payload
from .observability import observed
from .resolution_drafts import (
    InvestigationPlan, ResolutionDraft, attach_refund_recommendation,
    build_investigation_plan, build_refund_recommendation,
)
from .evidence_grounding import (
    EvidenceGrounding, GroundedResponse, apply_grounded_response, attach_grounding_citations,
    build_evidence_grounding, policy_query_for,
)


class GraphState(Contract):
    request: AgentRequest
    memory_started: bool = False
    steps: int = Field(default=0, strict=True, ge=0, le=32)
    messages: tuple[LLMMessage, ...] = ()
    allowed_names: tuple[str, ...] = ()
    decision: ToolCall | Finish | None = None
    staged_event: AuditEntry | None = None
    history: tuple[AuditEntry, ...] = ()
    pending: dict[UUID, bool] = Field(default_factory=dict)
    created: bool = False
    refund_requested: bool = False
    refund_outcome: str | None = None
    resolution_draft: ResolutionDraft | None = None
    model_resolution_draft: ResolutionDraft | None = None
    investigation_plan: InvestigationPlan | None = None
    model_investigation_plan: InvestigationPlan | None = None
    knowledge_grounding: EvidenceGrounding | None = None
    generated_resolution_draft: ResolutionDraft | None = None
    tool_calls: int = Field(default=0, strict=True, ge=0, le=32)
    error: str | None = None
    final_result: AgentResult | None = None
    node_path: tuple[str, ...] = ()


class GraphResolutionAgent:
    def __init__(self, llm: StructuredLLM, tools: LocalTools, config: AgentConfig | None = None,
                 *, memory: ConversationService | None = None,
                 enable_refund_wording: bool = False,
                 deterministic_fallback_on_invalid_response: bool = False) -> None:
        self._llm = llm
        self._tools = tools
        self._config = config or AgentConfig()
        self._memory = memory
        # The local/scripted path deliberately retains its existing no-model
        # refund routing.  Production Bedrock may supply prose, never rules.
        self._enable_refund_wording = enable_refund_wording
        # A schema-invalid Bedrock completion can be safely replaced only after
        # load_case has built the immutable, grounded draft.  Keep this opt-in
        # so the generic/local graph continues to fail closed for regressions.
        self._deterministic_fallback_on_invalid_response = deterministic_fallback_on_invalid_response
        graph = StateGraph(GraphState)
        for name in ("load_case", "evaluate_refund", "reason", "execute_tool", "record_result", "finalize", "fail_safely"):
            graph.add_node(name, getattr(self, "_" + name))
        graph.add_edge(START, "load_case")
        graph.add_conditional_edges("load_case", self._after_load_case,
                                    {name: name for name in ("fail_safely", "evaluate_refund", "reason")})
        graph.add_conditional_edges("evaluate_refund", self._after_refund_evaluation,
                                    {"fail_safely": "fail_safely", "proposal_created": "finalize",
                                     "finalize": "finalize"})
        graph.add_conditional_edges("reason", self._after_reason,
                                    {name: name for name in ("fail_safely", "finalize", "execute_tool")})
        graph.add_edge("execute_tool", "record_result")
        graph.add_conditional_edges("record_result", lambda s: "fail_safely" if s.error else "reason",
                                    {name: name for name in ("fail_safely", "reason")})
        graph.add_edge("finalize", END)
        graph.add_edge("fail_safely", END)
        self._graph = graph.compile()  # Conversation text is separate from graph state persistence.

    @staticmethod
    def _update(state: GraphState, node: str, **changes) -> dict:
        changes["node_path"] = (*state.node_path, node)
        # Validate outgoing updates too; LangGraph validates state on entry to nodes.
        validated = GraphState.model_validate({
            **{key: getattr(state, key) for key in GraphState.model_fields}, **changes,
        })
        return {key: getattr(validated, key) for key in changes}

    def run(self, request: AgentRequest) -> AgentResult:
        return self.run_state(request).final_result

    @observed("agent.graph")
    def run_state(self, request: AgentRequest) -> GraphState:
        """Run fresh state only; callers cannot inject history, counters, or resume a write."""
        state = GraphState(request=AgentRequest.model_validate(request))
        # One reasoning step uses at most three nodes, plus load and terminal nodes.
        # The business step budget is enforced separately before every LLM invocation.
        with tracing_context(enabled=False):
            try:
                for snapshot in self._graph.stream(
                    {"request": state.request}, stream_mode="values",
                    config={"recursion_limit": 3 * self._config.max_steps + 5, "callbacks": []},
                ):
                    state = GraphState.model_validate(snapshot)
            except GraphRecursionError:
                state = GraphState.model_validate({**state.model_dump(), "error": "GRAPH_LIMIT"})
            except Exception:
                state = GraphState.model_validate({**state.model_dump(), "error": "GRAPH_ERROR"})
        if state.final_result is None:
            state = GraphState.model_validate({
                **state.model_dump(), **self._fail_safely(state),
            })
        if state.memory_started:
            try:
                finish_memory(self._memory, state.request.conversation_id, state.request.case_id,
                              state.final_result.customer_response)
            except ConversationError as failure:
                event = AuditEntry(step=state.steps, error=failure.code)
                state = GraphState.model_validate({**state.model_dump(), "error": failure.code,
                    "history": (*state.history, event),
                    "final_result": self._result(state, (*state.history, event), failure.code)})
        return state

    def _load_case(self, state: GraphState) -> dict:
        call = ToolCall(kind="tool", name="get_case", arguments={"case_id": str(state.request.case_id)})
        try:
            result = self._tools.invoke(call.name, call.arguments)
        except Exception:
            return self._update(state, "load_case", staged_event=AuditEntry(step=0, decision=call, error="TOOL_ERROR"),
                                error="TOOL_ERROR")
        event = AuditEntry(step=0, decision=call, result=result)
        if not result.ok:
            return self._update(state, "load_case", staged_event=event, error=result.error.code)
        try:
            resolution_draft = self._tools.build_resolution_draft(
                state.request.case_id, state.request.message,
            )
            investigation_plan = build_investigation_plan(
                resolution_draft,
                request_text=" ".join((result.data.subject, result.data.description, state.request.message)),
                refund_requested=state.request.resolution_intent == "refund",
                max_tool_calls=self._config.max_steps,
            )
        except Exception:
            resolution_draft = None
            investigation_plan = None
        try:
            check_payload(state.request.model_dump(mode="json"))
            if resolution_draft is None:
                raise ValueError("Resolution draft is unavailable")
            product_skus = self._tools.case_order_skus(state.request.case_id)
            knowledge_grounding = build_evidence_grounding(
                resolution_draft,
                self._tools.retrieve_grounded_policy(
                    policy_query_for(resolution_draft.issue_type, product_skus)),
                product_skus=product_skus,
            )
        except SecurityError as failure:
            return self._update(state, "load_case", history=(event,), error=failure.code,
                                staged_event=AuditEntry(step=0, error=failure.code))
        except GuardrailError as failure:
            return self._update(state, "load_case", history=(event,), error=failure.code,
                                staged_event=AuditEntry(step=0, error=failure.code))
        except Exception:
            # Do not proceed to model reasoning without citable application-owned
            # policy context.  No citation can be fabricated on this path.
            return self._update(state, "load_case", history=(event,), error="KNOWLEDGE_UNAVAILABLE",
                                staged_event=AuditEntry(step=0, error="KNOWLEDGE_UNAVAILABLE"))
        try:
            conversation_id, memory_context = begin_memory(
                self._memory, state.request.conversation_id, state.request.case_id, state.request.message)
        except (ConversationError, SecurityError, GuardrailError) as failure:
            return self._update(state, "load_case", history=(event,), error=failure.code,
                                staged_event=AuditEntry(step=0, error=failure.code))
        request = AgentRequest(**{**state.request.model_dump(), "conversation_id": conversation_id})
        # The provider receives only schemas authorized by the immutable plan.
        # Server-side dispatch still checks the same allowlist independently.
        all_descriptions = self._tools.describe()
        descriptions = [item for item in all_descriptions if (
            investigation_plan is None or item.name in investigation_plan.allowed_tools
        )]
        instructions = agent_instructions(descriptions)
        messages = [LLMMessage(role="system", content=instructions)]
        if memory_context is not None:
            messages.append(LLMMessage(role="user", content=memory_context))
        messages.append(LLMMessage(role="user", content=json.dumps({
            "request": request.model_dump(mode="json"), "case_result": result.model_dump(mode="json"),
            "grounded_resolution_draft": (resolution_draft.model_dump(mode="json")
                                          if resolution_draft is not None else None),
            "grounded_investigation_plan": (investigation_plan.model_dump(mode="json")
                                              if investigation_plan is not None else None),
            "grounded_policy_evidence": knowledge_grounding.model_dump(mode="json"),
        })))
        return self._update(state, "load_case", history=(event,), request=request,
                            memory_started=conversation_id is not None,
                            refund_requested=request.resolution_intent == "refund",
                            allowed_names=tuple(item.name for item in descriptions), messages=tuple(messages),
                            resolution_draft=resolution_draft, investigation_plan=investigation_plan,
                            knowledge_grounding=knowledge_grounding)

    @staticmethod
    def _after_load_case(state: GraphState) -> str:
        if state.error:
            return "fail_safely"
        return "evaluate_refund" if state.refund_requested else "reason"

    @staticmethod
    def _after_refund_evaluation(state: GraphState) -> str:
        if state.error:
            return "fail_safely"
        return "proposal_created" if state.created and any(state.pending.values()) else "finalize"

    def _evaluate_refund(self, state: GraphState) -> dict:
        """Resolve refund eligibility without allowing a model decision to bypass evidence."""
        history = list(state.history)
        order_call = ToolCall(kind="tool", name="get_order",
                              arguments={"order_id": str(state.history[0].result.data.order_id)})
        try:
            order_result = self._tools.invoke(order_call.name, order_call.arguments)
        except Exception:
            return self._update(state, "evaluate_refund", error="TOOL_ERROR",
                                staged_event=AuditEntry(step=state.steps, decision=order_call, error="TOOL_ERROR"))
        history.append(AuditEntry(step=state.steps, decision=order_call, result=order_result))
        if not order_result.ok:
            return self._update(state, "evaluate_refund", history=tuple(history), error=order_result.error.code)

        selected = next((item for item in order_result.data.items if item.sku == state.request.refund_sku), None)
        if selected is None:
            recommendation = build_refund_recommendation(
                sku=str(state.request.refund_sku), purchased=False,
            )
            draft = (attach_refund_recommendation(state.resolution_draft, recommendation)
                     if state.resolution_draft is not None else None)
            return self._update(state, "evaluate_refund", history=tuple(history),
                                resolution_draft=draft,
                                refund_outcome="Refund request remains unresolved: selected SKU is not in the case-bound order.")
        assessment_call = ToolCall(kind="tool", name="assess_refund_eligibility",
                                   arguments={"case_id": str(state.request.case_id), "sku": selected.sku})
        try:
            assessment = self._tools.invoke(assessment_call.name, assessment_call.arguments)
        except Exception:
            return self._update(state, "evaluate_refund", history=tuple(history), error="TOOL_ERROR",
                                staged_event=AuditEntry(step=state.steps, decision=assessment_call, error="TOOL_ERROR"))
        history.append(AuditEntry(step=state.steps, decision=assessment_call, result=assessment))
        if not assessment.ok:
            if assessment.error.code in ("CONFLICT", "NOT_FOUND"):
                recommendation = build_refund_recommendation(sku=str(selected.sku))
                draft = (attach_refund_recommendation(state.resolution_draft, recommendation)
                         if state.resolution_draft is not None else None)
                draft = self._refund_wording(state, draft)
                return self._create_refund_review(state, history, action="refund_investigation",
                    rationale=("Human investigation required: trusted delivery evidence or policy confirmation "
                               "is unavailable for the selected case-bound purchased item. "
                               "This is not refund approval."), resolution_draft=draft)
            return self._update(state, "evaluate_refund", history=tuple(history), error=assessment.error.code)
        evidence = assessment.data
        recommendation = build_refund_recommendation(sku=str(selected.sku), assessment=evidence)
        draft = (attach_refund_recommendation(state.resolution_draft, recommendation)
                 if state.resolution_draft is not None else None)
        draft = self._refund_wording(state, draft)
        if not evidence.refund_eligible or evidence.denial_reasons or evidence.evidence_id is None:
            reasons = ", ".join(sorted(set(evidence.denial_reasons))) or "refund_eligibility_not_established"
            return self._create_refund_review(state, history, action="refund_investigation",
                rationale=("Human investigation required: trusted evidence does not confirm refund eligibility "
                           f"({reasons}). This is not refund approval."), resolution_draft=draft)
        return self._create_refund_review(state, history, action="refund",
            rationale="Server-side refund eligibility evidence requires human review.",
            evidence_id=evidence.evidence_id, resolution_draft=draft)

    def _refund_wording(self, state: GraphState, draft: ResolutionDraft | None) -> ResolutionDraft | None:
        """Optionally polish a completed Python assessment without delegating it.

        This bounded single call is enabled only for the configured Bedrock
        runtime.  It receives the same immutable grounding as normal graph
        reasoning; malformed, unsupported, or unavailable output leaves the
        deterministic assessment untouched.
        """
        if (not self._enable_refund_wording or draft is None
                or state.knowledge_grounding is None or not state.messages):
            return draft
        try:
            context = json.loads(state.messages[-1].content)
            context["grounded_resolution_draft"] = draft.model_dump(mode="json")
            answer = self._llm.generate(LLMRequest(
                messages=(*state.messages[:-1], LLMMessage(role="user", content=json.dumps(context))),
                max_output_tokens=self._config.max_output_tokens,
            ), AgentDecision)
            if not answer.ok:
                return draft
            return apply_grounded_response(draft, state.knowledge_grounding,
                                           answer.data.grounded_response) or draft
        except Exception:
            return draft

    def _create_refund_review(self, state: GraphState, history: list[AuditEntry], *,
                              action: str, rationale: str, evidence_id: UUID | None = None,
                              resolution_draft: ResolutionDraft | None = None) -> dict:
        arguments = {"case_id": str(state.request.case_id), "action": action, "rationale": rationale}
        if evidence_id is not None:
            arguments["eligibility_evidence_id"] = str(evidence_id)
        proposal_call = ToolCall(kind="tool", name="create_resolution_proposal", arguments=arguments)
        try:
            proposal_result = self._tools.invoke(proposal_call.name, proposal_call.arguments)
        except Exception:
            return self._update(state, "evaluate_refund", history=tuple(history), error="TOOL_ERROR",
                                staged_event=AuditEntry(step=state.steps, decision=proposal_call, error="TOOL_ERROR"))
        proposal_event = AuditEntry(step=state.steps, decision=proposal_call, result=proposal_result)
        history.append(proposal_event)
        if not proposal_result.ok:
            return self._update(state, "evaluate_refund", history=tuple(history), error=proposal_result.error.code)
        proposal = proposal_result.data
        if proposal.case_id != state.request.case_id or proposal.status.value != "PENDING_REVIEW":
            return self._update(state, "evaluate_refund", history=tuple(history), error="INVALID_PROPOSAL_STATUS")
        return self._update(state, "evaluate_refund", history=tuple(history), created=True,
                            pending={**state.pending, proposal.id: True},
                            resolution_draft=resolution_draft or state.resolution_draft)

    @staticmethod
    def _after_reason(state: GraphState) -> str:
        if state.error:
            return "fail_safely"
        return "finalize" if isinstance(state.decision, Finish) else "execute_tool"

    def _reason(self, state: GraphState) -> dict:
        if state.steps >= self._config.max_steps:
            return self._update(state, "reason", error="STEP_LIMIT")
        step = state.steps + 1
        try:
            answer = self._llm.generate(LLMRequest(messages=state.messages,
                                       max_output_tokens=self._config.max_output_tokens), AgentDecision)
        except Exception:
            return self._update(state, "reason", steps=step, error="LLM_ERROR",
                                staged_event=AuditEntry(step=step, error="LLM_ERROR"))
        if not answer.ok:
            if (self._deterministic_fallback_on_invalid_response
                    and answer.code == "INVALID_RESPONSE"
                    and answer.invalid_response_stage == "output_schema"):
                # Strict validation has already rejected the provider output.
                # Do not normalize or reinterpret it; complete from the
                # application-owned draft and retain the sanitized failure in
                # the audit history.  Provider-envelope failures still fail
                # closed and enter normal recovery handling.
                return self._update(
                    state, "reason", steps=step, decision=Finish(kind="final"),
                    history=(*state.history, AuditEntry(
                        step=step, llm_failure=answer, error="INVALID_RESPONSE")),
                )
            return self._update(state, "reason", steps=step, error=answer.code,
                                staged_event=AuditEntry(step=step, llm_failure=answer))
        changes = {"steps": step, "decision": answer.data.decision}
        # Knowledge retrieval is application-owned during case loading and is
        # also available as an explicitly allowlisted investigation tool.  A
        # second model-selected search does not add authority or evidence; in
        # production it can become a self-reinforcing Bedrock loop (the model
        # keeps searching instead of finishing).  Preserve the attempted
        # decision in the audit trail, but complete from the deterministic
        # grounded draft rather than spending the remaining step budget.
        if (isinstance(answer.data.decision, ToolCall)
                and answer.data.decision.name == "search_knowledge"
                and any(entry.decision is not None
                        and isinstance(entry.decision, ToolCall)
                        and entry.decision.name == "search_knowledge"
                        for entry in state.history)):
            changes["decision"] = Finish(kind="final")
            changes["history"] = (*state.history, AuditEntry(
                step=step, decision=answer.data.decision, error="REPEATED_TOOL_CALL"))
        if answer.data.resolution_draft == state.resolution_draft:
            changes["model_resolution_draft"] = answer.data.resolution_draft
        if answer.data.investigation_plan == state.investigation_plan:
            changes["model_investigation_plan"] = answer.data.investigation_plan
        generated = (apply_grounded_response(
            state.resolution_draft, state.knowledge_grounding, answer.data.grounded_response,
        ) if state.resolution_draft is not None and state.knowledge_grounding is not None else None)
        if generated is not None:
            changes["generated_resolution_draft"] = generated
        return self._update(state, "reason", **changes)

    def _execute_tool(self, state: GraphState) -> dict:
        call = state.decision
        # A proposal is always for the immutable, authorized request case.  The
        # model may choose a proposed action and rationale, but never its target.
        if isinstance(call, ToolCall) and call.name == "create_resolution_proposal":
            call = call.model_copy(update={"arguments": {
                **call.arguments, "case_id": str(state.request.case_id),
            }})
        error = None
        if not isinstance(call, ToolCall):
            error = "INVALID_RESPONSE"
        elif call.name not in state.allowed_names:
            error = "UNKNOWN_TOOL"
        elif (state.investigation_plan is not None
              and call.name not in state.investigation_plan.allowed_tools):
            error = "UNKNOWN_TOOL"
        elif state.investigation_plan is not None and state.tool_calls >= state.investigation_plan.max_tool_calls:
            error = "STEP_LIMIT"
        elif call.name == "create_resolution_proposal":
            if state.created:
                error = "PROPOSAL_LIMIT"
            elif call.arguments.get("action") in ("refund", "refund_investigation") and not state.refund_requested:
                error = "REFUND_INTENT_REQUIRED"
        if error:
            return self._update(state, "execute_tool", error=error,
                                staged_event=AuditEntry(step=state.steps, decision=call, error=error))
        try:
            # The Phase 5 dispatcher validates its existing input models BEFORE calling services.
            result = self._tools.invoke(call.name, call.arguments)
        except Exception:
            return self._update(state, "execute_tool", error="TOOL_ERROR",
                                staged_event=AuditEntry(step=state.steps, decision=call, error="TOOL_ERROR"))
        return self._update(state, "execute_tool",
                            staged_event=AuditEntry(step=state.steps, decision=call, result=result))

    def _record_result(self, state: GraphState) -> dict:
        event = state.staged_event
        if event is None:
            return self._update(state, "record_result", error="GRAPH_ERROR")
        changes = {"history": (*state.history, event), "staged_event": None}
        result = event.result
        if state.error:
            return self._update(state, "record_result", **changes)
        if result is None:
            return self._update(state, "record_result", **changes, error="GRAPH_ERROR")
        if not result.ok:
            return self._update(state, "record_result", **changes, error=result.error.code)
        changes["tool_calls"] = state.tool_calls + 1
        call = event.decision
        if call.name in ("create_resolution_proposal", "get_proposal_status"):
            record = result.data.model_dump(mode="json")
            if record["case_id"] != str(state.request.case_id):
                return self._update(state, "record_result", **changes, error="CASE_MISMATCH")
            changes["pending"] = {**state.pending, UUID(record["id"]): record["status"] == "PENDING_REVIEW"}
            if call.name == "create_resolution_proposal":
                changes["created"] = True
                if record["status"] != "PENDING_REVIEW":
                    return self._update(state, "record_result", **changes, error="INVALID_PROPOSAL_STATUS")
        changes["messages"] = (*state.messages,
            LLMMessage(role="assistant", content=AgentDecision(decision=call).model_dump_json()),
            LLMMessage(role="user", content=json.dumps({"tool_result": result.model_dump(mode="json")})),
        )
        if state.steps >= self._config.max_steps:
            changes["error"] = "STEP_LIMIT"
        return self._update(state, "record_result", **changes)

    @staticmethod
    def resolution_draft_for_state(state: GraphState) -> ResolutionDraft | None:
        """Return the validated wording that is safe to project from a checkpoint."""
        if state.final_result is not None and state.final_result.resolution_draft is not None:
            return state.final_result.resolution_draft
        if state.generated_resolution_draft is not None:
            return state.generated_resolution_draft
        if state.model_resolution_draft == state.resolution_draft:
            return state.model_resolution_draft
        return state.resolution_draft

    @staticmethod
    def admin_resolution_draft_for_state(state: GraphState) -> ResolutionDraft | None:
        """Add retriever-owned citation metadata only at the admin read projection.

        The fallback draft held by the graph remains identical to the existing
        deterministic contract.  This keeps graph/Phase 8 parity intact while
        allowing the staff-only API to show the policy sources that were
        actually retrieved for the run.
        """
        draft = GraphResolutionAgent.resolution_draft_for_state(state)
        if draft is None or state.knowledge_grounding is None:
            return draft
        return attach_grounding_citations(draft, state.knowledge_grounding)

    @staticmethod
    def _result(state: GraphState, history: tuple[AuditEntry, ...], error: str | None = None) -> AgentResult:
        ids = tuple(key for key, value in state.pending.items() if value)
        draft = (state.generated_resolution_draft
                 or (state.model_resolution_draft
                     if state.model_resolution_draft == state.resolution_draft
                     else state.resolution_draft))
        message = "Informational results only. No refund, return, replacement, or inventory action was executed."
        if ids:
            message = ("Proposal(s) recorded as PENDING_REVIEW require separate human review: "
                       + ", ".join(map(str, ids)) + ". No action was executed.")
        elif state.refund_outcome:
            message = state.refund_outcome + " No action was executed."
        elif draft is not None:
            # The deterministic draft is customer-safe by construction.  A
            # model's wording reaches this point only after grounding checks.
            # AI recommendations do not mutate the case or staff communication.
            message = draft.customer_facing_response
        if error:
            message = "The review could not be completed. " + message
        return AgentResult(status="FAILED" if error else "HUMAN_REVIEW_REQUIRED" if ids else "INFORMATIONAL",
                           conversation_id=state.request.conversation_id if state.memory_started else None,
                           customer_response=message, steps_used=state.steps, history=history,
                           pending_proposal_ids=ids,
                           resolution_draft=draft,
                           investigation_plan=(state.model_investigation_plan
                                               if state.model_investigation_plan == state.investigation_plan
                                               else state.investigation_plan),
                           error=error)

    def _finalize(self, state: GraphState) -> dict:
        early_fallback = any(
            entry.error == "REPEATED_TOOL_CALL"
            or (entry.error == "INVALID_RESPONSE"
                and entry.llm_failure is not None
                and entry.llm_failure.invalid_response_stage == "output_schema")
            for entry in state.history
        )
        history = (*state.history, AuditEntry(
            step=state.steps, decision=state.decision,
            error="EARLY_DETERMINISTIC_FALLBACK" if early_fallback else None,
        ))
        return self._update(state, "finalize", history=history, final_result=self._result(state, history))

    def _fail_safely(self, state: GraphState) -> dict:
        error = state.error or "GRAPH_ERROR"
        # If the engine failed between execution and recording, retain a completed
        # tool outcome (especially a pending write) without executing it again.
        if state.staged_event and state.staged_event.result and state.staged_event.result.ok:
            recovered = GraphState.model_validate({**state.model_dump(), "error": None})
            state = GraphState.model_validate({**recovered.model_dump(), **self._record_result(recovered)})
        history = (*state.history, state.staged_event) if state.staged_event else state.history
        return self._update(state, "fail_safely", history=history, staged_event=None, error=error,
                            final_result=self._result(state, history, error))
