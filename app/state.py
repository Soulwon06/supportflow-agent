from typing import List, Mapping, TypedDict


class SupportState(TypedDict):
    user_message: str
    intent: str
    result: str
    error_log: str
    execution_log: List[str]

    need_confirmation: bool
    confirmed: bool
    step_count: int

    role: str
    user_id: int
    permissions: List[str]

    order_id: str
    current_order_id: str
    order_history: List[str]
    refund_amount: float
    order_total: float
    refundable_amount: float
    refund_reason: str
    idempotency_key: str
    pending_action: str
    missing_fields: List[str]
    refund_cancelled: bool
    terminal_status: str

    # LLM routing/generation metadata. These fields are informational and do
    # not grant permission to call tools or bypass HITL.
    routing_source: str
    routing_error_classification: str
    rag_corpus: str
    retrieved_source_ids: List[str]
    database_source_ids: List[str]
    sop_source_ids: List[str]
    rag_source: str
    rag_error_classification: str
    needs_clarification: bool
    clarification_message: str

    # Safe RAG answerability metadata. These are current-turn fields and are
    # reset by create_turn_input; they never alter business authorization state.
    retrieved_evidence_id: str
    retrieved_evidence_corpus: str
    retrieved_evidence_document_id: str
    retrieved_evidence_chunk_id: str | None
    retrieved_evidence_text: str
    retrieved_evidence_title: str
    retrieved_evidence_category: str
    retrieved_evidence_source_section: str
    retrieved_evidence_version: str
    rerank_score: float | None
    answerability_status: str
    supported_facts: List[str]
    missing_facts: List[str]
    generation_skipped: bool
    generation_called: bool
    safe_fallback_reason: str


def create_initial_state(
    user_message: str,
    role: str = "customer_service",
) -> SupportState:
    """
    创建一份完整、合法的 SupportFlow 初始 State。

    所有进入 LangGraph 的新请求，
    都应该通过这个函数创建 Initial State。

    这样可以避免不同入口各自初始化 State，
    导致字段遗漏或默认值不一致。
    """

    return {
        "user_message": user_message,
        "intent": "",
        "result": "",
        "error_log": "",
        "execution_log": [],

        "need_confirmation": False,
        "confirmed": False,
        "step_count": 0,

        "role": role,
        "user_id": 0,
        "permissions": [],

        "order_id": "",
        # These are checkpoint-owned business context fields. They are
        # updated by router_node and preserved by create_turn_input().
        "current_order_id": "",
        "order_history": [],
        "refund_amount": 0.0,
        "order_total": 0.0,
        "refundable_amount": 0.0,
        "refund_reason": "",
        "idempotency_key": "",
        "pending_action": "",
        "missing_fields": [],
        "refund_cancelled": False,
        "terminal_status": "",

        "routing_source": "",
        "routing_error_classification": "",
        "rag_corpus": "customer_support",
        "retrieved_source_ids": [],
        "database_source_ids": [],
        "sop_source_ids": [],
        "rag_source": "",
        "rag_error_classification": "",
        "needs_clarification": False,
        "clarification_message": "",

        "retrieved_evidence_id": "",
        "retrieved_evidence_corpus": "",
        "retrieved_evidence_document_id": "",
        "retrieved_evidence_chunk_id": None,
        "retrieved_evidence_text": "",
        "retrieved_evidence_title": "",
        "retrieved_evidence_category": "",
        "retrieved_evidence_source_section": "",
        "retrieved_evidence_version": "",
        "rerank_score": None,
        "answerability_status": "",
        "supported_facts": [],
        "missing_facts": [],
        "generation_skipped": False,
        "generation_called": False,
        "safe_fallback_reason": "",
    }


def create_turn_input(
    user_message: str,
    role: str = "customer_service",
    user_id: int = 0,
    existing_state: Mapping[str, object] | None = None,
) -> SupportState:
    """Create a new turn input without overwriting checkpoint business context.

    LangGraph validates the full state schema on invocation. Persistent order
    context is copied from the checkpoint for an existing conversation, while
    current-turn fields such as intent, result, execution_log, and step_count
    are reset.
    """
    state = create_initial_state(user_message=user_message, role=role)
    state["user_id"] = user_id
    if existing_state:
        state["user_id"] = int(existing_state.get("user_id") or user_id or 0)
        state["current_order_id"] = str(
            existing_state.get("current_order_id") or ""
        )
        state["order_history"] = list(
            existing_state.get("order_history") or []
        )
        state["pending_action"] = str(existing_state.get("pending_action") or "")
        state["missing_fields"] = list(existing_state.get("missing_fields") or [])
        state["order_id"] = str(existing_state.get("order_id") or "")
        state["order_total"] = float(existing_state.get("order_total") or 0.0)
        state["refundable_amount"] = float(
            existing_state.get("refundable_amount") or 0.0
        )
        state["refund_amount"] = float(existing_state.get("refund_amount") or 0.0)
        state["refund_reason"] = str(existing_state.get("refund_reason") or "")
        state["permissions"] = list(existing_state.get("permissions") or [])
        # An idempotency key belongs to one active refund operation, not to
        # the whole conversation. Preserve it only while a refund is waiting
        # for HITL confirmation; terminal turns must start with a fresh key.
        if (
            state["pending_action"] == "refund"
            and bool(existing_state.get("need_confirmation"))
        ):
            state["idempotency_key"] = str(
                existing_state.get("idempotency_key") or ""
            )
        else:
            state["idempotency_key"] = ""
    return state
