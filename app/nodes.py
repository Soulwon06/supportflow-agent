import re
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from langgraph.types import interrupt

from .state import SupportState
from .config import get_settings
from .retriever import (
    CorpusName,
    CorpusUnavailableError,
    load_real_reranker,
    real_rerank,
    real_reranker_status,
    retrieve_from_corpus,
    retriever_startup_profile,
)
from .database import get_inventory, get_order, get_production
from .tool_dispatcher import dispatch_tool, has_tool_permission
from .llm_client import LLMProvider, ProviderError, build_provider
from .llm_service import LLMService
from .observability import emit_progress, get_current_trace
from .operations import create_request


_provider_override: LLMProvider | None = None
_llm_service: LLMService | None = None

# Only SF-prefixed IDs are valid when written explicitly. Bare numeric IDs are
# accepted only in an order-number context, so refund amounts cannot become
# order IDs (for example, 10000 in "退款10000元").
SF_ORDER_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])SF\d{4,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
NUMERIC_ORDER_ID_PATTERN = re.compile(
    r"(?:订单号|订单号码|订单)\s*(?P<order_id>\d{4,})",
    re.IGNORECASE,
)
ORDINAL_PATTERN = re.compile(r"第\s*(?P<number>一|二|两|[12])\s*个")
ORDINALS = {"一": 0, "二": 1, "两": 1, "1": 0, "2": 1}
SAFE_ABSTENTION_MESSAGE = "当前知识库没有足够信息回答这个问题，建议联系人工客服进一步确认。"
SAFE_NO_EVIDENCE_MESSAGE = "当前没有找到足够相关资料可靠回答这个问题，建议联系人工客服进一步确认。"
SAFE_RETRIEVAL_FAILURE_MESSAGE = "检索系统暂时不可用，未能完成资料检索，请稍后重试。"
SAFE_PROVIDER_FAILURE_MESSAGE = "回答服务暂时不可用，未生成未经验证的回答，请稍后重试。"
MANUFACTURING_DEMO_NOTICE = "（以下内容来自教学演示规范，不代表真实企业正式标准。）"

INTENT_LABELS = {
    "knowledge": "知识咨询",
    "manufacturing_knowledge": "制造规范咨询",
    "quality_mixed": "订单质检与处理建议",
    "order": "订单查询",
    "logistics": "物流查询",
    "refund": "退款申请",
    "inventory": "库存查询",
    "production": "生产进度",
    "quality": "质检结果",
    "quality_submit": "质检申报",
    "admin_approval": "管理员审批",
    "outbound_status": "出库状态",
    "order_entry": "上单申请",
    "production_submit": "生产报工申请",
    "outbound_submit": "出库申请",
    "unknown": "其他问题",
}


def configure_llm_provider(provider: LLMProvider | None) -> None:
    """Inject a provider for tests or an embedding application."""
    global _provider_override, _llm_service
    _provider_override = provider
    _llm_service = LLMService(provider) if provider is not None else None


def _get_llm_service() -> LLMService:
    global _llm_service
    if _llm_service is None:
        provider = _provider_override or build_provider()
        _llm_service = LLMService(provider)
    return _llm_service


def _keyword_fallback_intent(message: str) -> str:
    """Small, explicit safety net used only when semantic classification fails."""
    if _is_admin_approval_request(message):
        return "admin_approval"
    if _is_outbound_status_query(message):
        return "outbound_status"
    if _is_quality_mixed_query(message):
        return "quality_mixed"
    if _is_manufacturing_knowledge_query(message):
        return "manufacturing_knowledge"
    if _is_quality_submit_query(message):
        return "quality_submit"
    if re.search(r"退款政策|退款期限|退货政策|退货条件", message):
        return "knowledge"
    if re.search(
        r"退款\s*\d|退\s*\d|全部退款|全额退款|退款.*订单|申请退款|我要退款|^\s*退款\s*$",
        message,
    ):
        return "refund"
    if re.search(r"物流|快递|配送|运到哪里|现在到哪|到哪里了", message):
        return "logistics"
    if re.search(r"库存|现有多少|可用数量|还有多少|余量", message):
        return "inventory"
    if re.search(r"提交|申报|申请", message) and re.search(r"出库", message) and not re.search(r"查询|查一下|状态|记录", message):
        return "outbound_submit"
    if re.search(r"提交|申报|报工|完成", message) and re.search(r"报工|完成", message) and not re.search(r"查询|查一下|状态|记录|多少|进度", message):
        return "production_submit"
    if re.search(r"质检|检验|不良品|不合格率|合格率", message):
        return "quality"
    if re.search(r"已完成|生产进度|合格数量|生产任务|完成了多少|报工", message):
        return "production"
    if re.search(r"上单|下单|创建订单|提交订单", message):
        return "order_entry"
    if re.search(r"订单|SF\d{4,}|查一下|查询|查", message):
        return "order"
    if re.search(r"质量问题|商品质量|瑕疵|损坏|坏了|售后", message):
        return "knowledge"
    return "unknown"


def _is_manufacturing_knowledge_query(message: str) -> bool:
    """Recognize stable manufacturing guidance without touching write flows."""

    return bool(
        (
            re.search(r"生产前|开工前|产前", message)
            and re.search(r"检查|核对|准备|需要", message)
        )
        or (
            re.search(r"不合格品|不良品", message)
            and re.search(r"隔离|记录|复检|处理|怎么|如何|规范", message)
        )
        or (
            re.search(r"卷轴", message)
            and re.search(r"包装|规格|标签|批次|核对", message)
        )
        or (
            re.search(r"出库", message)
            and re.search(r"规范|流程|核对|交接|审批|经过", message)
            and not re.search(r"提交|申报|直接出库", message)
        )
        or (
            re.search(r"订单|报工|质检|出库", message)
            and re.search(r"审批流程|分别由谁审批|审批顺序|申请流程", message)
        )
    )


def _is_quality_mixed_query(message: str) -> bool:
    """Recognize only the narrow read-only order-quality-plus-SOP question."""
    has_order = bool(
        SF_ORDER_ID_PATTERN.search(message)
        or NUMERIC_ORDER_ID_PATTERN.search(message)
        or re.search(r"(?<![A-Za-z0-9])[A-Z]{1,4}\d{3,}(?![A-Za-z0-9])", message)
    )
    has_quality_fact = bool(re.search(r"质检|检验|不合格|不通过|合格", message))
    asks_reason = bool(re.search(r"为什么|原因|未通过|不通过|不合格", message))
    asks_treatment = bool(re.search(r"处理|怎么办|怎么做|如何|应该|隔离|记录|复检", message))
    return has_order and has_quality_fact and asks_reason and asks_treatment


def _is_quality_submit_query(message: str) -> bool:
    """Recognize a quality-report write request, not a read-only query."""
    return bool(
        re.search(r"质检|检验|检查", message)
        and re.search(r"提交|申报|报工", message)
        and not re.search(r"查询|查一下|结果|状态|多少|记录", message)
    )


def _is_admin_approval_request(message: str) -> bool:
    """Detect approval commands without ever executing them from chat."""
    normalized = re.sub(r"\s+", "", message)
    if re.search(r"审批流程|审批顺序|怎么审批|如何审批|审批规范", normalized):
        return False
    if re.fullmatch(r"(?:同意|批准|审批|拒绝|驳回)", normalized):
        return True
    return bool(
        re.search(r"(?:批准|审批|同意|拒绝|驳回)", normalized)
        and re.search(r"申请|订单|出库|报工|质检", normalized)
    )


def _is_outbound_status_query(message: str) -> bool:
    """Recognize read-only shipment progress, not an outbound application."""
    if re.search(r"提交|申报|申请|出库申请|直接出库", message):
        return False
    if re.search(r"物流|快递|配送|运输|到哪|当前位置", message):
        return False
    return bool(
        re.search(r"出库|发货|发出|已发|未发|发了|发过", message)
        and re.search(r"了吗|多少|数量|状态|剩余|还有|查看|多少件|没有", message)
    )


def _write_negation_kind(message: str) -> str:
    """Classify explicit write prohibitions before any LLM/tool decision.

    This is intentionally narrow: the guard protects high-risk writes but does
    not treat every occurrence of "不要" as a cancellation.
    """
    normalized = re.sub(r"\s+", "", message)
    action = r"(?:提交|申报|执行|出库|报工|质检|检验|创建|下单|上单|退款|申请)"
    if re.search(r"(?:不要|别)(?:忘记|忘了)", normalized):
        return ""
    if re.search(r"(?:为什么|为何|怎么|如何)不能", normalized) and re.search(action, normalized):
        return "query"
    if re.search(
        r"如果.+?(?:不足|不够|不满足|不允许|失败|异常).+?(?:不要|别|不应|不可).*(?:提交|执行|出库|报工|创建|下单|上单|申请)",
        normalized,
    ):
        return "conditional"
    if re.search(
        rf"(?:不要|别|无需|不用|暂不|先别|取消)(?:本次|这次|当前)?(?:直接|马上|立即)?{action}",
        normalized,
    ):
        return "blocked"
    return ""


def _write_guard_result(state: SupportState, kind: str):
    if kind == "query":
        message = "这是查询问题，系统不会提交或执行任何申请。请说明你想查询的具体条件。"
    elif kind == "conditional":
        message = "检测到条件式的暂不执行要求，系统不会直接创建申请；请明确确认满足条件后是否提交。"
    else:
        message = "已按你的要求停止本次提交，未创建申请，也未执行任何业务写入。"
    return {
        "result": message,
        "error_log": "",
        "terminal_status": "NEED_USER_INPUT",
        "write_guarded": True,
        "pending_action": "",
        "missing_fields": [],
        "need_confirmation": False,
        "confirmed": False,
        "routing_error_classification": "WRITE_NEGATION_GUARDED",
        "execution_log": state["execution_log"] + ["write_negation_guard"],
    }


def extract_order_ids(message: str) -> list[str]:
    """Extract valid order IDs without confusing monetary amounts for IDs."""
    ids = [match.upper() for match in SF_ORDER_ID_PATTERN.findall(message)]
    for match in NUMERIC_ORDER_ID_PATTERN.finditer(message):
        order_id = match.group("order_id")
        if order_id.upper() not in ids:
            ids.append(order_id.upper())
    return ids


def extract_ordinal_index(message: str) -> int | None:
    match = ORDINAL_PATTERN.search(message)
    if not match:
        return None
    return ORDINALS[match.group("number")]


def _append_unique(values: list[str], value: str) -> list[str]:
    result = list(values)
    if value and value not in result:
        result.append(value)
    return result


def _clarification_for_orders(order_history: list[str]) -> str:
    if order_history:
        return "你是指订单 " + " 还是 ".join(order_history) + "？"
    return "请提供订单号，我才能继续处理。"


def _resolve_order_context(
    state: SupportState,
    message: str,
    intent: str,
) -> tuple[str, list[str], bool, str]:
    """Resolve order focus deterministically from explicit text and checkpoint."""
    history = list(state.get("order_history") or [])
    current = str(state.get("current_order_id") or "")
    explicit_ids = extract_order_ids(message)

    if explicit_ids:
        target = explicit_ids[-1]
        for order_id in explicit_ids:
            history = _append_unique(history, order_id)
        return target, history, False, ""

    ordinal_index = extract_ordinal_index(message)
    if ordinal_index is not None:
        if ordinal_index < len(history):
            return history[ordinal_index], history, False, ""
        return "", history, True, _clarification_for_orders(history)

    # A sensitive request without a target must not silently choose among
    # several previously discussed orders. Explicit ordinal references remain
    # safe and deterministic.
    if (
        intent == "refund"
        and state.get("pending_action") == "refund"
        and state.get("order_id")
        and _is_pending_refund_continuation(state, message)
    ):
        # Card actions use the backend-owned order captured when the card was
        # created; they must not be re-resolved from the whole history.
        return str(state["order_id"]), history, False, ""

    if intent == "refund" and len(history) > 1:
        return "", history, True, _clarification_for_orders(history)

    if current:
        return current, history, False, ""
    if len(history) == 1:
        return history[0], history, False, ""
    if intent in {
        "order",
        "logistics",
        "refund",
        "production",
        "quality",
        "production_submit",
        "outbound_submit",
        "outbound_status",
    }:
        return "", history, True, _clarification_for_orders(history)
    return "", history, False, ""


def _target_order_id(state: SupportState, message: str) -> str:
    explicit_ids = extract_order_ids(message)
    if explicit_ids:
        return explicit_ids[-1]
    return str(
        state.get("current_order_id")
        or state.get("order_id")
        or ""
    )


def _is_obvious_new_intent(message: str) -> bool:
    """Messages with a clear new request must not be consumed by a slot."""
    return bool(
        re.search(
            r"政策|规则|查询|查一下|查查|物流|快递|配送|到哪|状态|多久|是什么|怎么|质量|售后|订单信息",
            message,
            re.IGNORECASE,
        )
    )


def _is_pending_refund_continuation(
    state: SupportState,
    message: str,
) -> bool:
    """Return whether the message clearly supplies a pending refund slot."""
    if state.get("pending_action") != "refund":
        return False
    if _is_obvious_new_intent(message):
        return False
    if re.search(r"取消|不要了|算了", message):
        return True
    if re.search(r"全部退款|全额退款|退全部|全部退", message):
        return True
    missing_fields = set(state.get("missing_fields") or [])
    if "order_id" in missing_fields:
        return bool(
            extract_order_ids(message)
            or extract_ordinal_index(message) is not None
        )
    if "amount" in missing_fields:
        amount_pattern = r"(?:[¥￥]\s*)?[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
        return bool(
            re.fullmatch(
                rf"\s*{amount_pattern}\s*(?:元|块|人民币)?\s*",
                message,
                re.IGNORECASE,
            )
            or re.search(
                rf"(?:退款|退(?!款))\s*{amount_pattern}",
                message,
                re.IGNORECASE,
            )
        )
    return False


def _extract_operation_quantity(message: str) -> int | None:
    """Extract a production/outbound quantity without treating SF1001 as 1001."""
    patterns = (
        r"(?:完成|报工|生产|出库|数量|申报)\s*(\d+)\s*(?:个|卷|只|件|pcs)?",
        r"^\s*(\d+)\s*(?:个|卷|只|件|pcs)?\s*$",
    )
    for pattern in patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _is_pending_manufacturing_continuation(state: SupportState, message: str) -> bool:
    action = state.get("pending_action")
    if action not in {"production_submit", "outbound_submit", "order_entry", "quality_submit"}:
        return False
    if _is_obvious_new_intent(message):
        return False
    if action == "order_entry":
        return bool(
            CUSTOMER_PATTERN.search(message)
            or _extract_sku(message)
            or re.search(r"\d+\s*(?:个|卷|只|pcs)", message, re.IGNORECASE)
            or re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", message)
        )
    if action == "quality_submit":
        return bool(
            extract_order_ids(message)
            or re.search(r"检验|合格|不合格|不良|数量", message)
        )
    return bool(extract_order_ids(message) or _extract_operation_quantity(message))


# ============================================================
# 1. Router Node
# ============================================================

def router_node(state: SupportState):
    """
    根据用户消息判断应该进入哪个业务流程。
    """

    message = state["user_message"]
    routing_source = "llm"
    routing_error = ""
    emit_progress("intent", "running", "正在理解你的问题…")

    negation_kind = _write_negation_kind(message)
    # Existing refund slot cancellation is a controlled continuation of an
    # unsubmitted refund draft. It is handled by refund_prepare_node; a new
    # negated write must never be treated as withdrawal of a submitted request.
    allow_pending_refund_cancel = (
        state.get("pending_action") == "refund"
        and bool(re.search(r"取消|不要了|算了", message))
    )
    if negation_kind and not allow_pending_refund_cancel:
        return {
            "intent": "unknown",
            "routing_source": "deterministic_write_negation_guard",
            "routing_error_classification": "WRITE_NEGATION_GUARDED",
            "terminal_status": "NEED_USER_INPUT",
            **_write_guard_result(state, negation_kind),
        }

    if _is_admin_approval_request(message):
        return {
            "intent": "admin_approval",
            "routing_source": "deterministic_admin_approval_guard",
            "routing_error_classification": "CHAT_APPROVAL_DISABLED",
            "result": "审批操作请通过管理员审批看板执行，聊天入口不会直接批准或拒绝申请。",
            "terminal_status": "NEED_USER_INPUT",
            "write_guarded": True,
            "pending_action": "",
            "missing_fields": [],
            "need_confirmation": False,
            "confirmed": False,
            "execution_log": state["execution_log"] + ["admin_approval_guard"],
        }

    if _is_pending_refund_continuation(state, message):
        # Only an unambiguous slot completion bypasses fresh intent routing.
        intent = "refund"
        routing_source = "pending_action_resume"
    elif _is_pending_manufacturing_continuation(state, message):
        intent = str(state["pending_action"])
        routing_source = "pending_action_resume"
    else:
        try:
            classification = _get_llm_service().classify_intent(message)
            intent = classification.intent.value
        except ProviderError as exc:
            # External LLM failure must not prevent deterministic support flows.
            intent = _keyword_fallback_intent(message)
            routing_source = "keyword_fallback"
            routing_error = exc.classification
        except Exception:
            intent = _keyword_fallback_intent(message)
            routing_source = "keyword_fallback"
            routing_error = "UNEXPECTED_ROUTING_ERROR"

        quality_mixed_query = _is_quality_mixed_query(message)
        quality_submit_query = _is_quality_submit_query(message)
        outbound_status_query = _is_outbound_status_query(message)
        manufacturing_knowledge_query = _is_manufacturing_knowledge_query(message)
        quality_query = (
            re.search(r"质检|检验|不良品|不合格率|合格率", message)
            or (
                re.search(r"合格", message)
                and re.search(r"不合格", message)
            )
        )
        if outbound_status_query:
            intent = "outbound_status"
            routing_source = "deterministic_outbound_status_override"
        elif quality_submit_query:
            intent = "quality_submit"
            routing_source = "deterministic_quality_submit_override"
        elif quality_mixed_query:
            intent = "quality_mixed"
            routing_source = "deterministic_mixed_quality_override"
        elif manufacturing_knowledge_query:
            intent = "manufacturing_knowledge"
            routing_source = "deterministic_manufacturing_knowledge_override"
        elif quality_query and not re.search(r"提交|申报|报工", message):
            intent = "quality"
            routing_source = "deterministic_manufacturing_override"
        elif (
            re.search(r"出库", message)
            and (
                _extract_operation_quantity(message) is not None
                or re.search(r"提交|申报|申请", message)
            )
            and not re.search(r"查询|查一下|状态|记录|多少|进度", message)
        ):
            intent = "outbound_submit"
            routing_source = "deterministic_manufacturing_override"
        elif re.search(r"报工|申报完成|提交完成|完成\s*\d+", message) and not re.search(r"查询|查一下|状态|记录|多少|进度", message):
            intent = "production_submit"
            routing_source = "deterministic_manufacturing_override"
        elif (
            re.search(r"订单", message)
            and re.search(r"需求|需要|已完成|完成|还差|剩余", message)
            and not re.search(r"生产|报工|质检|检验|出库|物流|库存", message)
        ):
            # Explicit order-progress wording must not be downgraded to the
            # production tool merely because it mentions completed quantity.
            intent = "order"
            routing_source = "deterministic_manufacturing_override"

        # Keep common colloquial refund forms deterministic if a provider
        # returns the generic unknown class.
        if intent == "unknown":
            fallback_intent = _keyword_fallback_intent(message)
            if fallback_intent != "unknown":
                intent = fallback_intent
                routing_source = "keyword_fallback"

    current_order_id, order_history, needs_clarification, clarification_message = (
        _resolve_order_context(state, message, intent)
    )

    # Refund is a high-risk action. Authorization is decided only from the
    # trusted backend role, never from user text or LLM output, and must happen
    # before order lookup, amount collection, prepare, or HITL confirmation.
    if intent == "refund" and not has_tool_permission(
        state["role"],
        "refund_order",
        permissions=state.get("permissions"),
    ):
        return {
            "intent": intent,
            "step_count": 1,
            "current_order_id": current_order_id
            or state.get("current_order_id", ""),
            "order_history": order_history,
            "order_id": current_order_id or state.get("order_id", ""),
            "needs_clarification": False,
            "clarification_message": "",
            "pending_action": "",
            "missing_fields": [],
            "refund_cancelled": False,
            "need_confirmation": False,
            "routing_source": "backend_authorization",
            "routing_error_classification": "PERMISSION_DENIED",
            "result": "当前角色无权发起退款操作。",
            "terminal_status": "PERMISSION_DENIED",
            "error_log": (
                f"PERMISSION_DENIED: 角色 {state['role']} "
                "无权调用工具 refund_order"
            ),
            "execution_log": state["execution_log"] + ["router_node"],
        }

    rag_corpus = (
        CorpusName.manufacturing_demo.value
        if intent in {"manufacturing_knowledge", "quality_mixed"}
        else CorpusName.customer_support.value
        if intent == "knowledge"
        else ""
    )
    trace = get_current_trace()
    if trace is not None:
        trace.metadata.update({"route": intent, "rag_corpus": rag_corpus or None})

    emit_progress(
        "intent",
        "completed",
        f"已理解问题：{INTENT_LABELS.get(intent, '其他问题')}",
        intent=intent,
        routing_source=routing_source,
    )

    pending_action = intent if intent in {"refund", "production_submit", "outbound_submit", "order_entry", "quality_submit"} else ""
    missing_fields = ["order_id"] if intent in {"refund", "production_submit", "outbound_submit", "quality_submit"} and needs_clarification else []

    pending_refund_continuation = _is_pending_refund_continuation(
        state,
        message,
    )
    refund_context = {
        "order_id": current_order_id or state.get("order_id", ""),
        "order_total": state.get("order_total", 0.0),
        "refundable_amount": state.get("refundable_amount", 0.0),
        "refund_amount": state.get("refund_amount", 0.0),
        "refund_reason": state.get("refund_reason", ""),
        "idempotency_key": (
            state.get("idempotency_key", "")
            if pending_refund_continuation
            else ""
        ),
    }
    if intent != "refund":
        refund_context = {
            "order_id": "",
            "order_total": 0.0,
            "refundable_amount": 0.0,
            "refund_amount": 0.0,
            "refund_reason": "",
            "idempotency_key": "",
        }

    return {
        "intent": intent,
        "step_count": 1,
        "current_order_id": current_order_id or state.get("current_order_id", ""),
        "order_history": order_history,
        "order_id": current_order_id or state.get("order_id", ""),
        "needs_clarification": needs_clarification,
        "clarification_message": clarification_message,
        "pending_action": pending_action,
        "missing_fields": missing_fields,
        "refund_cancelled": False,
        "terminal_status": "",
        "write_guarded": False,
        "rag_corpus": rag_corpus,
        "retrieved_source_ids": [],
        **refund_context,
        "routing_source": routing_source,
        "routing_error_classification": routing_error,
        "execution_log": state["execution_log"]
        + ["router_node"],
    }


# ============================================================
# 2. Knowledge Node
# ============================================================

def _retrieve_reranked(
    query: str,
    *,
    corpus: CorpusName = CorpusName.customer_support,
    role: str = "customer_service",
) -> list[dict]:
    """Run the frozen local-only Hybrid/RRF -> BGE path for one Corpus."""
    trace = get_current_trace()
    settings = get_settings()
    reranker_model_path = Path(settings.reranker_model_path)
    reranker_was_loaded = bool(real_reranker_status().get("loaded"))
    reranker_init_span = (
        trace.start_span(
            "reranker_initialization",
            metadata={"model_path": str(reranker_model_path)},
        )
        if trace
        else None
    )
    if not reranker_was_loaded:
        emit_progress("reranker_init", "running", "正在加载相关性排序模型…")
    try:
        load_real_reranker(reranker_model_path, device=settings.reranker_device)
        if reranker_init_span is not None:
            reranker_init_span.metadata.update(
                {
                    "cache_hit": reranker_was_loaded,
                    "initialization_seconds": real_reranker_status().get(
                        "initialization_seconds"
                    ),
                }
            )
            reranker_init_span.finish(success=True)
        if not reranker_was_loaded:
            emit_progress(
                "reranker_init",
                "completed",
                "相关性排序模型已就绪",
                cache_hit=reranker_was_loaded,
            )
    except Exception:
        if reranker_init_span is not None:
            reranker_init_span.finish(success=False, error="RERANKER_INIT_ERROR")
        emit_progress(
            "reranker_init",
            "failed",
            "相关性排序模型暂时不可用，请稍后重试。",
        )
        raise

    retrieval_span = trace.start_span(
        "retrieval",
        metadata={"candidate_k": 5, "rag_corpus": corpus.value},
    ) if trace else None
    retrieval_profile = {}
    if trace is not None:
        trace.metadata["retriever_startup_profile"] = retriever_startup_profile()
    emit_progress("retrieval", "running", "正在搜索相关资料…")
    try:
        candidates = retrieve_from_corpus(
            query,
            corpus=corpus,
            role=role,
            candidate_k=5,
            top_k=5,
            require_dense=True,
            use_reranker=False,
            profile=retrieval_profile,
        )
        if retrieval_span is not None:
            retrieval_span.metadata["candidate_ids"] = [item.get("id") for item in candidates]
            retrieval_span.metadata["retrieved_source_ids"] = [
                item.get("id") for item in candidates
            ]
            retrieval_span.metadata["profile"] = retrieval_profile
            retrieval_span.finish(success=True)
        if trace is not None:
            trace.metadata["retrieval_profile"] = retrieval_profile
        emit_progress(
            "retrieval",
            "completed",
            f"已找到 {len(candidates)} 条候选资料",
            candidate_count=len(candidates),
        )
    except Exception as exc:
        if retrieval_span is not None:
            retrieval_span.metadata["profile"] = retrieval_profile
            retrieval_span.finish(success=False, error="RETRIEVAL_ERROR")
        if trace is not None:
            trace.metadata["retrieval_profile"] = retrieval_profile
        emit_progress(
            "retrieval",
            "failed",
            "检索系统暂时不可用，请稍后重试。",
        )
        raise exc

    rerank_span = trace.start_span(
        "rerank",
        metadata={
            "candidate_k": 5,
            "model_path": str(reranker_model_path),
            "rag_corpus": corpus.value,
        },
    ) if trace else None
    emit_progress("rerank", "running", "正在筛选最相关的资料…")
    try:
        results = real_rerank(query, candidates, top_k=3)
        if rerank_span is not None:
            rerank_span.metadata["top1_id"] = results[0].get("id") if results else None
            rerank_span.metadata["top1_score"] = results[0].get("rerank_score") if results else None
            rerank_span.finish(success=True)
        if results:
            emit_progress(
                "rerank",
                "completed",
                f"已找到最相关资料：{results[0].get('title') or '知识库资料'}",
                evidence_id=results[0].get("id"),
                evidence_title=results[0].get("title"),
                rerank_score=results[0].get("rerank_score"),
            )
        return results
    except Exception:
        if rerank_span is not None:
            rerank_span.finish(success=False, error="RERANKER_ERROR")
        emit_progress(
            "rerank",
            "failed",
            "相关资料筛选暂时不可用，请稍后重试。",
        )
        raise


def _rag_metadata(result: dict) -> dict:
    return {
        "retrieved_evidence_id": str(result.get("id") or ""),
        "retrieved_evidence_corpus": str(result.get("corpus") or ""),
        "retrieved_evidence_document_id": str(result.get("document_id") or ""),
        "retrieved_evidence_chunk_id": result.get("chunk_id"),
        "retrieved_evidence_text": str(result.get("text") or result.get("content") or ""),
        "retrieved_evidence_title": str(result.get("title") or ""),
        "retrieved_evidence_category": str(result.get("category") or ""),
        "retrieved_evidence_source_section": str(result.get("source_section") or ""),
        "retrieved_evidence_version": str(result.get("source_version") or ""),
        "rerank_score": result.get("rerank_score"),
    }


def _safe_rag_fallback(state: SupportState, reason: str) -> dict:
    if reason == "NO_EVIDENCE":
        message = SAFE_NO_EVIDENCE_MESSAGE
    elif reason in {"RETRIEVAL_OR_RERANKER_ERROR", "RETRIEVAL_ERROR"}:
        message = SAFE_RETRIEVAL_FAILURE_MESSAGE
    else:
        message = SAFE_PROVIDER_FAILURE_MESSAGE
    return {
        "result": message,
        "error_log": f"RAG_SAFE_FALLBACK: {reason}",
        "rag_source": "safe_fallback",
        "rag_error_classification": reason,
        "answerability_status": "",
        "supported_facts": [],
        "missing_facts": [],
        "generation_skipped": True,
        "generation_called": False,
        "safe_fallback_reason": reason,
        "terminal_status": "SAFE_FALLBACK",
        "rag_corpus": state.get("rag_corpus") or "",
        "retrieved_source_ids": [],
        "execution_log": state["execution_log"] + ["knowledge_node"],
    }

def knowledge_node(state: SupportState):
    """
    Knowledge path: local Hybrid/RRF -> BGE -> answerability judge ->
    constrained generation or deterministic abstention.

    This path never falls back to an ungrounded or raw retrieval answer. Any
    retrieval, judge, or generator failure returns a safe abstention.
    """

    query = state["user_message"]

    try:
        corpus = CorpusName(
            state.get("rag_corpus") or CorpusName.customer_support.value
        )
        role = str(state.get("role") or "customer_service")
        if corpus is CorpusName.customer_support:
            # Preserve the historical one-argument test/embedding hook while
            # the implementation itself uses the explicit customer Corpus.
            results = _retrieve_reranked(query)
        else:
            results = _retrieve_reranked(query, corpus=corpus, role=role)

        if not results:
            return _safe_rag_fallback(state, "NO_EVIDENCE")

        top_result = results[0]
        metadata = _rag_metadata(top_result)
        source_ids = [str(item.get("id") or "") for item in results if item.get("id")]
        metadata["retrieved_source_ids"] = source_ids
        metadata["rag_corpus"] = corpus.value
        trace = get_current_trace()
        generation_attempted = False
        if trace is not None:
            trace.metadata["rag"] = {
                **metadata,
                "candidate_count": len(results),
            }
            trace.metadata["route"] = state.get("intent")
            trace.metadata["rag_corpus"] = corpus.value

        try:
            evidence_text = str(top_result.get("text") or top_result.get("content") or "")
            emit_progress("judge", "running", "正在确认资料是否足够回答…")
            judgment = _get_llm_service().judge_answerability(query, evidence_text)
            status = judgment.status.value
            answerability_message = {
                "SUPPORTED": "资料足够，可以回答",
                "PARTIAL": "资料只能回答部分问题",
                "UNSUPPORTED": "当前资料不足以可靠回答",
            }.get(status, "已完成资料充分性判断")
            emit_progress(
                "judge",
                "completed",
                answerability_message,
                answerability_status=status,
            )
            if trace is not None:
                trace.metadata["answerability_status"] = status

            common = {
                **metadata,
                "answerability_status": status,
                "supported_facts": judgment.supported_facts,
                "missing_facts": judgment.missing_facts,
                "generation_called": False,
                "safe_fallback_reason": "",
                "rag_corpus": corpus.value,
                "retrieved_source_ids": source_ids,
                "terminal_status": "SUCCESS",
                "error_log": "",
                "rag_error_classification": "",
                "execution_log": state["execution_log"] + ["knowledge_node"],
            }

            if status == "UNSUPPORTED":
                return {
                    **common,
                    "result": SAFE_ABSTENTION_MESSAGE,
                    "rag_source": "answerability_abstention",
                    "generation_skipped": True,
                    "terminal_status": "SAFE_FALLBACK",
                }

            generation_attempted = True
            emit_progress("generation", "running", "正在生成回答…")
            generated = _get_llm_service().generate_constrained_grounded(
                query,
                [top_result],
                status=status,
                supported_facts=judgment.supported_facts,
                missing_facts=judgment.missing_facts,
            )
            allowed_source_ids = {str(top_result.get("id") or "")}
            if any(source_id not in allowed_source_ids for source_id in generated.source_ids):
                raise ProviderError(
                    "SCHEMA_VALIDATION",
                    "grounded answer referenced an unknown evidence id",
                )
            result = generated.answer
            if corpus is CorpusName.manufacturing_demo:
                result = f"{MANUFACTURING_DEMO_NOTICE}\n{result}"
            if generated.source_ids:
                result += "\n来源：" + "、".join(generated.source_ids)
            emit_progress("generation", "completed", "回答已生成")
            return {
                **common,
                "result": result,
                "rag_source": "llm",
                "generation_skipped": False,
                "generation_called": True,
            }
        except ProviderError as exc:
            failed_stage = "generation" if generation_attempted else "judge"
            emit_progress(
                failed_stage,
                "failed",
                "回答服务暂时不可用，未生成未经验证的回答，请稍后重试。",
                error_classification=exc.classification,
            )
            return {
                **(common if "common" in locals() else metadata),
                "result": SAFE_PROVIDER_FAILURE_MESSAGE,
                "error_log": f"RAG_SAFE_FALLBACK: {exc.classification}",
                "rag_source": "safe_fallback",
                "rag_error_classification": exc.classification,
                "generation_skipped": not generation_attempted,
                "generation_called": generation_attempted,
                "safe_fallback_reason": exc.classification,
                "terminal_status": "SAFE_FALLBACK",
                "execution_log": state["execution_log"] + ["knowledge_node"],
            }

    except Exception:
        return _safe_rag_fallback(state, "RETRIEVAL_OR_RERANKER_ERROR")


# ============================================================
# 3. Order Node
# ============================================================

def order_node(state: SupportState):
    """
    从用户消息提取订单号，
    通过 Dispatcher 调用 Order Tool。
    """

    message = state["user_message"]

    order_id = _target_order_id(state, message)

    if not order_id:
        return {
            "result": "没有识别到订单号，请提供订单号。",
            "error_log": "INVALID_ARGUMENT: missing order_id",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["order_node"],
        }

    tool_result = dispatch_tool(
        "query_order",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=order_id,
    )

    if not tool_result.success:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "result": tool_result.error_message,
            "error_log": (
                f"{tool_result.error_code}: "
                f"{tool_result.error_message}"
            ),
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["order_node"],
        }

    order = tool_result.data

    return {
        "order_id": order_id,
        "current_order_id": order_id,
        "result": (
            f"订单 {order['order_id']} 查询成功："
            f"商品为 {order['product']}，"
            f"需求 {order['ordered_quantity']} 个，"
            f"已完成 {order['completed_quantity']} 个，"
            f"剩余 {max(0, int(order['ordered_quantity']) - int(order['completed_quantity']))} 个，"
            f"状态为 {order['status']}，"
            f"金额 ¥{order['amount']}。"
        ),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"]
        + ["order_node"],
    }


# ============================================================
# 4. Logistics Node
# ============================================================

def logistics_node(state: SupportState):
    """
    查询物流。

    优先从当前用户消息提取订单号；
    如果当前消息没有订单号，
    则尝试复用 State 中上一轮保存的 order_id。
    """

    message = state["user_message"]

    order_id = _target_order_id(state, message)

    # 当前消息和历史 State 都没有
    if not order_id:
        return {
            "result": "没有识别到订单号，请提供订单号。",
            "error_log": "INVALID_ARGUMENT: missing order_id",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["logistics_node"],
        }

    tool_result = dispatch_tool(
        "query_logistics",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=order_id,
    )

    if not tool_result.success:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "result": tool_result.error_message,
            "error_log": (
                f"{tool_result.error_code}: "
                f"{tool_result.error_message}"
            ),
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["logistics_node"],
        }

    logistics = tool_result.data

    return {
        "order_id": order_id,
        "current_order_id": order_id,
        "result": (
            f"订单 {logistics['order_id']} "
            f"物流状态：{logistics['status']}，"
            f"当前位置：{logistics['location']}。"
        ),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"]
        + ["logistics_node"],
    }


def outbound_status_node(state: SupportState):
    """Read trusted order quantities without creating an outbound request."""
    if not has_tool_permission(
        state["role"], "query_order", state.get("permissions")
    ):
        return {
            "result": "当前账号没有查询订单出库状态的权限。",
            "error_log": "PERMISSION_DENIED",
            "terminal_status": "PERMISSION_DENIED",
            "execution_log": state["execution_log"] + ["outbound_status_node"],
        }

    order_id = _target_order_id(state, state["user_message"])
    if not order_id:
        return {
            "result": "没有识别到订单号，请提供订单号。",
            "error_log": "INVALID_ARGUMENT: missing order_id",
            "terminal_status": "NEED_USER_INPUT",
            "execution_log": state["execution_log"] + ["outbound_status_node"],
        }
    order = get_order(order_id)
    if not order:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "result": f"未找到订单 {order_id}，无法查询出库状态。",
            "error_log": "NOT_FOUND: ORDER_NOT_FOUND",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["outbound_status_node"],
        }

    ordered = int(order.get("ordered_quantity") or 0)
    shipped = int(order.get("shipped_quantity") or 0)
    remaining = max(0, ordered - shipped)
    if shipped <= 0:
        status = "尚未出库"
    elif remaining == 0:
        status = "已全部出库"
    else:
        status = "部分出库"
    return {
        "order_id": order_id,
        "current_order_id": order_id,
        "result": (
            f"订单 {order_id} 出库状态：{status}。"
            f"需求 {ordered} 个，已出库 {shipped} 个，剩余未出库 {remaining} 个。\n"
            "本次仅查询订单出库数量；如需运输位置和物流节点，请单独查询物流。"
        ),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"] + ["outbound_status_node"],
    }


# ============================================================
# 5. Manufacturing Business Nodes
# ============================================================

SKU_PATTERN = re.compile(r"REEL-\d+IN-(?:BLACK|CLEAR|ESD|WHITE)", re.IGNORECASE)
CUSTOMER_PATTERN = re.compile(r"CUST-\d{3}", re.IGNORECASE)


def _extract_sku(message: str) -> str | None:
    match = SKU_PATTERN.search(message)
    return match.group(0).upper() if match else None


def _format_inventory(items: list[dict]) -> str:
    lines = ["库存查询结果："]
    for item in items:
        lines.append(
            f"{item['sku']}（{item['product_name']}），"
            f"仓库 {item['warehouse_name']}，批次 {item['lot_no']}，"
            f"现有 {item['on_hand_quantity']} 个，"
            f"已预留 {item['reserved_quantity']} 个，"
            f"可用 {item['available_quantity']} 个。"
        )
    return "\n".join(lines)


def inventory_node(state: SupportState):
    message = state["user_message"]
    sku = _extract_sku(message)
    keyword = None if sku else next(
        (value for value in ("7英寸", "13英寸", "15英寸", "22英寸", "防静电", "透明", "黑色") if value in message),
        None,
    )
    tool_result = dispatch_tool(
        "query_inventory",
        role=state["role"],
        permissions=state.get("permissions"),
        sku=sku,
        keyword=keyword,
    )
    if not tool_result.success:
        return {
            "result": tool_result.error_message,
            "error_log": f"{tool_result.error_code}: {tool_result.error_message}",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["inventory_node"],
        }
    return {
        "result": _format_inventory(tool_result.data["items"]),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"] + ["inventory_node"],
    }


def production_node(state: SupportState):
    order_id = _target_order_id(state, state["user_message"])
    production_no_match = re.search(r"MO-[A-Z0-9-]+", state["user_message"], re.IGNORECASE)
    tool_result = dispatch_tool(
        "query_production",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=order_id or None,
        production_no=production_no_match.group(0).upper() if production_no_match else None,
    )
    if not tool_result.success:
        return {
            "result": tool_result.error_message,
            "error_log": f"{tool_result.error_code}: {tool_result.error_message}",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["production_node"],
        }
    lines = ["生产进度查询结果："]
    for item in tool_result.data["items"]:
        lines.append(
            f"生产任务 {item['production_no']}（订单 {item['order_no']}，{item['sku']}）："
            f"计划 {item['planned_quantity']} 个，已报工 {item['reported_quantity']} 个，"
            f"合格 {item['qualified_quantity']} 个，不合格 {item['rejected_quantity']} 个，"
            f"状态 {item['status']}。"
        )
    return {
        "result": "\n".join(lines),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"] + ["production_node"],
    }


def quality_node(state: SupportState):
    order_id = _target_order_id(state, state["user_message"])
    tool_result = dispatch_tool("query_quality", role=state["role"], permissions=state.get("permissions"), order_id=order_id or None)
    if not tool_result.success:
        return {"result": tool_result.error_message, "error_log": f"{tool_result.error_code}: {tool_result.error_message}", "terminal_status": "FAILED", "execution_log": state["execution_log"] + ["quality_node"]}
    lines = ["质检结果查询："]
    for item in tool_result.data["items"]:
        lines.append(f"订单 {item['order_no']}（{item.get('sku') or '产品'}）：检验 {item['inspected_quantity']} 个，合格 {item['qualified_quantity']} 个，不合格 {item['rejected_quantity']} 个，日期 {item['report_date']}。")
    return {"result": "\n".join(lines), "error_log": "", "terminal_status": "SUCCESS", "execution_log": state["execution_log"] + ["quality_node"]}


def _extract_quality_quantity(message: str, labels: str) -> int | None:
    match = re.search(
        rf"(?:{labels})\s*(?:数量)?\s*[:：]?\s*(-?\d+)",
        message,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def quality_submit_node(state: SupportState):
    """Create a pending quality-report request from explicit numeric slots."""
    guarded = _write_node_guard(state)
    if guarded:
        return guarded
    if not has_tool_permission(
        state["role"], "submit_quality_report", state.get("permissions")
    ):
        return {
            "result": "当前账号没有提交质检申报的权限。",
            "error_log": "PERMISSION_DENIED",
            "terminal_status": "PERMISSION_DENIED",
            "execution_log": state["execution_log"] + ["quality_submit_node"],
        }

    message = state["user_message"]
    order_id = _target_order_id(state, message).upper()
    inspected = _extract_quality_quantity(message, r"检验|检查|质检")
    qualified = _extract_quality_quantity(message, r"(?<!不)合格")
    rejected = _extract_quality_quantity(message, r"不合格|不良")
    if inspected is None:
        inspected = state.get("quality_inspected_quantity")
    if qualified is None:
        qualified = state.get("quality_qualified_quantity")
    if rejected is None:
        rejected = state.get("quality_rejected_quantity")

    missing = []
    if not order_id:
        missing.append("order_id")
    if inspected is None:
        missing.append("inspected_quantity")
    if qualified is None:
        missing.append("qualified_quantity")
    if rejected is None:
        missing.append("rejected_quantity")
    if missing:
        labels = {
            "order_id": "订单号",
            "inspected_quantity": "检验数量",
            "qualified_quantity": "合格数量",
            "rejected_quantity": "不合格数量",
        }
        return {
            "result": "提交质检申报还需要：" + "、".join(labels[item] for item in missing) + "。例如：提交质检 SF1001 检验100合格90不合格10。",
            "error_log": "",
            "terminal_status": "NEED_USER_INPUT",
            "pending_action": "quality_submit",
            "missing_fields": missing,
            "order_id": order_id,
            "current_order_id": order_id,
            "quality_inspected_quantity": inspected,
            "quality_qualified_quantity": qualified,
            "quality_rejected_quantity": rejected,
            "execution_log": state["execution_log"] + ["quality_submit_node"],
        }

    values = (int(inspected), int(qualified), int(rejected))
    if values[0] <= 0 or min(values[1], values[2]) < 0:
        return {
            "result": "质检数量必须合法：检验数量大于 0，合格和不合格数量不能为负数。",
            "error_log": "INVALID_ARGUMENT: QUALITY_QUANTITY_INVALID",
            "terminal_status": "FAILED",
            "pending_action": "",
            "missing_fields": [],
            "execution_log": state["execution_log"] + ["quality_submit_node"],
        }
    if values[1] + values[2] != values[0]:
        return {
            "result": "质检申报不合法：合格数量与不合格数量之和必须等于检验数量。",
            "error_log": "INVALID_ARGUMENT: QUALITY_QUANTITY_MISMATCH",
            "terminal_status": "FAILED",
            "pending_action": "",
            "missing_fields": [],
            "execution_log": state["execution_log"] + ["quality_submit_node"],
        }
    try:
        item = create_request(
            "quality_report",
            int(state.get("user_id") or 0),
            {
                "order_no": order_id,
                "inspected_quantity": values[0],
                "qualified_quantity": values[1],
                "rejected_quantity": values[2],
                "report_date": str(date.today()),
                "remark": "自然语言提交",
            },
        )
    except (ValueError, TypeError) as exc:
        return {
            "result": str(exc),
            "error_log": f"INVALID_ARGUMENT: {exc}",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["quality_submit_node"],
        }
    return {
        "result": f"质检申报已提交：{order_id}，检验 {values[0]} 个，合格 {values[1]} 个，不合格 {values[2]} 个，申请号 {item['request_no']}，等待管理员审批。",
        "error_log": "",
        "terminal_status": "PENDING_APPROVAL",
        "pending_action": "",
        "missing_fields": [],
        "execution_log": state["execution_log"] + ["quality_submit_node"],
    }


def _mixed_quality_order_id(state: SupportState) -> str:
    """Resolve a mixed-query order without treating an amount as an order ID."""
    resolved = _target_order_id(state, state["user_message"])
    if resolved:
        return resolved.upper()
    match = re.search(
        r"(?<![A-Za-z0-9])([A-Z]{1,4}\d{3,})(?![A-Za-z0-9])",
        state["user_message"],
        re.IGNORECASE,
    )
    return match.group(1).upper() if match else ""


def _mixed_quality_db_text(order_id: str, items: list[dict]) -> tuple[str, list[str], list[str]]:
    source_ids = []
    facts = []
    for item in items:
        source_id = f"db:quality:{item['order_no']}:{item['report_date']}"
        source_ids.append(source_id)
        facts.append(
            f"订单 {item['order_no']} 的质检记录：检验 {item['inspected_quantity']} 个，"
            f"合格 {item['qualified_quantity']} 个，不合格 {item['rejected_quantity']} 个，"
            f"记录日期 {item['report_date']}。"
        )
    rejected = sum(int(item.get("rejected_quantity") or 0) for item in items)
    if rejected > 0:
        facts.append(
            f"数据库记录显示存在 {rejected} 个不合格品，因此不能把该批次描述为全部通过。"
        )
    else:
        facts.append("数据库记录中的不合格数量为 0，未发现质检未通过事实。")
    facts.append("当前 quality_reports 表没有独立的缺陷原因字段，不能从数据库判断具体失败原因。")
    return "\n".join(facts), source_ids, facts


def quality_mixed_node(state: SupportState):
    """Read-only composition of trusted quality facts and manufacturing SOP evidence.

    The database remains the only source for order-specific facts.  The SOP is
    used only for general handling guidance.  No LLM generation or write tool
    is invoked on this path.
    """
    order_id = _mixed_quality_order_id(state)
    base = {
        "intent": "quality_mixed",
        "rag_corpus": CorpusName.manufacturing_demo.value,
        "database_source_ids": [],
        "sop_source_ids": [],
        "retrieved_source_ids": [],
        "generation_called": False,
        "generation_skipped": True,
        "answerability_status": "",
        "execution_log": state["execution_log"] + ["quality_mixed_node"],
    }
    if not order_id:
        return {
            **base,
            "result": "请提供明确订单号，我才能查询该订单的质检事实。",
            "terminal_status": "NEED_USER_INPUT",
            "error_log": "",
            "rag_source": "database_plus_manufacturing_sop",
        }

    db_result = dispatch_tool(
        "query_quality",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=order_id,
    )
    if not db_result.success:
        return {
            **base,
            "result": db_result.error_message,
            "error_log": f"{db_result.error_code}: {db_result.error_message}",
            "terminal_status": "PERMISSION_DENIED" if db_result.error_code == "PERMISSION_DENIED" else "FAILED",
            "rag_source": "database",
        }

    db_text, db_source_ids, db_facts = _mixed_quality_db_text(
        order_id,
        db_result.data["items"],
    )
    common = {
        **base,
        "database_source_ids": db_source_ids,
        "retrieved_source_ids": list(db_source_ids),
        "supported_facts": db_facts,
        "missing_facts": ["具体缺陷原因（数据库未记录）"],
        "order_id": order_id,
        "current_order_id": order_id,
        "error_log": "",
        "rag_source": "database",
        "terminal_status": "SUCCESS",
    }

    try:
        sop_results = _retrieve_reranked(
            "不合格品隔离记录复检处理规范",
            corpus=CorpusName.manufacturing_demo,
            role=str(state.get("role") or "employee"),
        )
    except CorpusUnavailableError as exc:
        if exc.reason == "NO_AUTHORIZED_ACTIVE_DOCUMENTS":
            return {
                **common,
                "result": db_text + "\n当前账号无权访问制造质检 SOP，因此不提供处理建议。",
                "rag_error_classification": "SOP_PERMISSION_DENIED",
            }
        return {
            **common,
            "result": db_text + "\n制造质检 SOP 当前不可用，未根据缺失资料推测处理方式。",
            "rag_source": "safe_fallback",
            "safe_fallback_reason": "SOP_RETRIEVAL_UNAVAILABLE",
            "rag_error_classification": "SOP_RETRIEVAL_UNAVAILABLE",
            "terminal_status": "SAFE_FALLBACK",
        }
    except Exception:
        return {
            **common,
            "result": db_text + "\n制造质检 SOP 当前不可用，未根据缺失资料推测处理方式。",
            "rag_source": "safe_fallback",
            "safe_fallback_reason": "SOP_RETRIEVAL_UNAVAILABLE",
            "rag_error_classification": "SOP_RETRIEVAL_UNAVAILABLE",
            "terminal_status": "SAFE_FALLBACK",
        }

    if not sop_results:
        return {
            **common,
            "result": db_text + "\n没有找到足够的制造质检 SOP 证据，未提供处理建议。",
            "rag_source": "safe_fallback",
            "safe_fallback_reason": "SOP_NO_EVIDENCE",
            "rag_error_classification": "SOP_NO_EVIDENCE",
            "terminal_status": "SAFE_FALLBACK",
        }

    top_sop = sop_results[0]
    sop_source_ids = [str(item.get("id") or "") for item in sop_results if item.get("id")]
    sop_text = str(top_sop.get("text") or top_sop.get("content") or "")
    return {
        **common,
        **_rag_metadata(top_sop),
        "retrieved_source_ids": db_source_ids + sop_source_ids,
        "sop_source_ids": sop_source_ids,
        "rag_source": "database_plus_manufacturing_sop",
        "result": (
            f"{db_text}\n"
            "具体失败原因未记录在当前质检数据库中，不能据此猜测。\n"
            f"根据制造质检演示规范《{top_sop.get('title') or '质检规范'}》：{sop_text}"
        ),
    }


def _manufacturing_submission_error(state: SupportState, message: str, missing: list[str]):
    action = state["intent"]
    return {
        "result": message,
        "error_log": "",
        "terminal_status": "NEED_USER_INPUT",
        "pending_action": action,
        "missing_fields": missing,
        "execution_log": state["execution_log"] + [f"{action}_node"],
    }


def _write_node_guard(state: SupportState):
    """Repeat the deterministic write guard at every high-risk node entry."""
    kind = _write_negation_kind(state["user_message"])
    if not kind:
        return None
    if (
        state.get("pending_action") == "refund"
        and re.search(r"取消|不要了|算了", state["user_message"])
    ):
        return None
    return _write_guard_result(state, kind)


def production_submit_node(state: SupportState):
    guarded = _write_node_guard(state)
    if guarded:
        return guarded
    if not has_tool_permission(state["role"], "submit_production_report", state.get("permissions")):
        return {"result": "当前账号没有提交生产报工的权限。", "error_log": "PERMISSION_DENIED", "terminal_status": "PERMISSION_DENIED", "execution_log": state["execution_log"] + ["production_submit_node"]}
    order_id = _target_order_id(state, state["user_message"]).upper()
    quantity = _extract_operation_quantity(state["user_message"])
    missing = []
    if not order_id:
        missing.append("order_id")
    if quantity is None:
        missing.append("reported_quantity")
    if missing:
        fields = "、".join("订单号" if item == "order_id" else "完成数量" for item in missing)
        return _manufacturing_submission_error(state, f"提交生产报工还需要：{fields}。例如：SF1001 完成 100 个。", missing)
    # Validate against trusted production data before creating a request. The
    # approval path repeats this check because the order may change while the
    # request is waiting for an administrator.
    production_no_match = re.search(
        r"MO-[A-Z0-9-]+", state["user_message"], re.IGNORECASE
    )
    production_no = production_no_match.group(0).upper() if production_no_match else ""
    production_rows = get_production(
        production_no=production_no or None,
        order_no=None if production_no else order_id,
    )
    if not production_rows:
        return {
            "result": f"未找到订单 {order_id} 对应的生产任务，无法提交报工。",
            "error_log": "INVALID_ARGUMENT: PRODUCTION_TASK_NOT_FOUND",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["production_submit_node"],
        }
    production = production_rows[0]
    order_id = str(production["order_no"]).upper()
    remaining = int(production["planned_quantity"]) - int(production["reported_quantity"])
    if quantity > remaining:
        return {
            "result": f"报工数量 {quantity} 个超过订单 {order_id} 当前可报工剩余数量 {remaining} 个。",
            "error_log": "INVALID_ARGUMENT: PRODUCTION_QUANTITY_EXCEEDED",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["production_submit_node"],
        }
    try:
        item = create_request(
            "production_report",
            int(state.get("user_id") or 0),
            {
                "order_no": order_id,
                "production_no": production["production_no"],
                "reported_quantity": quantity,
                "qualified_quantity": quantity,
                "rejected_quantity": 0,
                "report_date": str(date.today()),
                "remark": "自然语言提交",
            },
        )
    except (ValueError, TypeError) as exc:
        return {"result": str(exc), "error_log": f"INVALID_ARGUMENT: {exc}", "terminal_status": "FAILED", "execution_log": state["execution_log"] + ["production_submit_node"]}
    return {"result": f"生产报工申请已提交：{order_id}，完成 {quantity} 个，申请号 {item['request_no']}，等待管理员审批。", "error_log": "", "terminal_status": "PENDING_APPROVAL", "pending_action": "", "missing_fields": [], "execution_log": state["execution_log"] + ["production_submit_node"]}


def outbound_submit_node(state: SupportState):
    guarded = _write_node_guard(state)
    if guarded:
        return guarded
    if not has_tool_permission(state["role"], "request_outbound", state.get("permissions")):
        return {"result": "当前账号没有提交出库申请的权限。", "error_log": "PERMISSION_DENIED", "terminal_status": "PERMISSION_DENIED", "execution_log": state["execution_log"] + ["outbound_submit_node"]}
    order_id = _target_order_id(state, state["user_message"]).upper()
    quantity = _extract_operation_quantity(state["user_message"])
    missing = []
    if not order_id:
        missing.append("order_id")
    if quantity is None:
        missing.append("quantity")
    if missing:
        fields = "、".join("订单号" if item == "order_id" else "出库数量" for item in missing)
        return _manufacturing_submission_error(state, f"提交出库申请还需要：{fields}。例如：SF1001 出库 100 个。", missing)
    # Submission-time checks improve feedback, while the administrator
    # approval path remains the final trusted revalidation boundary.
    order = get_order(order_id)
    if not order:
        return {
            "result": f"未找到订单 {order_id}，无法提交出库申请。",
            "error_log": "INVALID_ARGUMENT: ORDER_NOT_FOUND",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["outbound_submit_node"],
        }
    remaining = int(order["ordered_quantity"]) - int(order["shipped_quantity"])
    if quantity > remaining:
        return {
            "result": f"出库数量 {quantity} 个超过订单 {order_id} 当前剩余数量 {remaining} 个。",
            "error_log": "INVALID_ARGUMENT: OUTBOUND_QUANTITY_EXCEEDED",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["outbound_submit_node"],
        }
    inventory_rows = get_inventory(keyword=order.get("product"))
    available = sum(int(item["available_quantity"]) for item in inventory_rows)
    if quantity > available:
        return {
            "result": f"当前可用库存仅有 {available} 个，不能提交出库 {quantity} 个。",
            "error_log": "INVALID_ARGUMENT: OUTBOUND_INVENTORY_INSUFFICIENT",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["outbound_submit_node"],
        }
    try:
        item = create_request(
            "outbound_request",
            int(state.get("user_id") or 0),
            {"order_no": order_id, "quantity": quantity, "remark": "自然语言提交"},
        )
    except (ValueError, TypeError) as exc:
        return {"result": str(exc), "error_log": f"INVALID_ARGUMENT: {exc}", "terminal_status": "FAILED", "execution_log": state["execution_log"] + ["outbound_submit_node"]}
    return {"result": f"出库申请已提交：{order_id}，申请数量 {quantity} 个，申请号 {item['request_no']}，等待管理员审批。", "error_log": "", "terminal_status": "PENDING_APPROVAL", "pending_action": "", "missing_fields": [], "execution_log": state["execution_log"] + ["outbound_submit_node"]}


def order_entry_node(state: SupportState):
    guarded = _write_node_guard(state)
    if guarded:
        return guarded

    can_direct_create = has_tool_permission(
        state["role"], "create_sales_order", state.get("permissions")
    )
    can_submit = has_tool_permission(
        state["role"], "submit_order", state.get("permissions")
    )
    if not can_direct_create and not can_submit:
        return {
            "result": "当前账号没有提交订单申请的权限。",
            "error_log": "PERMISSION_DENIED",
            "terminal_status": "PERMISSION_DENIED",
            "pending_action": "",
            "missing_fields": [],
            "execution_log": state["execution_log"] + ["order_entry_node"],
        }

    message = state["user_message"]
    customer_code = (
        CUSTOMER_PATTERN.search(message).group(0).upper()
        if CUSTOMER_PATTERN.search(message)
        else str(state.get("order_customer_code") or "")
    )
    sku = _extract_sku(message) or str(state.get("order_sku") or "")
    quantity_match = re.search(r"(?<!\d)(\d+)\s*(?:个|卷|只|pcs)", message, re.IGNORECASE)
    quantity = int(quantity_match.group(1)) if quantity_match else state.get("order_quantity")
    date_match = re.search(r"20\d{2}[-/]\d{1,2}[-/]\d{1,2}", message)
    required_date = (
        date_match.group(0).replace("/", "-")
        if date_match
        else str(state.get("order_required_date") or "")
    )
    missing = []
    if not customer_code:
        missing.append("customer_code")
    if not sku:
        missing.append("sku")
    if quantity is None:
        missing.append("quantity")
    if not required_date:
        missing.append("required_date")
    if missing:
        labels = {
            "customer_code": "客户编号",
            "sku": "产品 SKU",
            "quantity": "需求数量",
            "required_date": "交期",
        }
        return {
            "result": "上单还需要：" + "、".join(labels[item] for item in missing) + "。例如：CUST-001 下单 REEL-7IN-BLACK 500个，交期 2026-09-30。",
            "error_log": "",
            "terminal_status": "NEED_USER_INPUT",
            "pending_action": "order_entry",
            "missing_fields": missing,
            "order_customer_code": customer_code,
            "order_sku": sku,
            "order_quantity": quantity,
            "order_required_date": required_date,
            "execution_log": state["execution_log"] + ["order_entry_node"],
        }

    if can_submit and not can_direct_create:
        try:
            item = create_request(
                "order_submission",
                int(state.get("user_id") or 0),
                {
                    "customer_code": customer_code,
                    "sku": sku,
                    "quantity": int(quantity),
                    "required_date": required_date,
                    "remark": "自然语言提交",
                },
            )
        except (ValueError, TypeError) as exc:
            return {
                "result": str(exc),
                "error_log": f"INVALID_ARGUMENT: {exc}",
                "terminal_status": "FAILED",
                "execution_log": state["execution_log"] + ["order_entry_node"],
            }
        return {
            "result": f"订单申请已提交：{customer_code}，产品 {sku}，数量 {quantity} 个，申请号 {item['request_no']}，等待管理员审批。",
            "error_log": "",
            "terminal_status": "PENDING_APPROVAL",
            "pending_action": "",
            "missing_fields": [],
            "execution_log": state["execution_log"] + ["order_entry_node"],
        }

    tool_result = dispatch_tool(
        "create_sales_order",
        role=state["role"],
        permissions=state.get("permissions"),
        customer_code=customer_code,
        sku=sku,
        quantity=int(quantity),
        required_date=required_date,
    )
    if not tool_result.success:
        return {
            "result": tool_result.error_message,
            "error_log": f"{tool_result.error_code}: {tool_result.error_message}",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"] + ["order_entry_node"],
        }
    order = tool_result.data
    return {
        "result": (
            f"上单已提交：订单 {order['order_id']}，客户 {order['customer_name']}，"
            f"产品 {order['sku']}，数量 {order['quantity']} 个，"
            f"预计交期 {order['required_date']}，金额 ¥{order['total_amount']:.2f}。"
        ),
        "error_log": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"] + ["order_entry_node"],
    }


# ============================================================
# 5. Refund Prepare Node
# ============================================================


def _extract_refund_amount(message: str) -> Decimal | None:
    """Extract a user-supplied amount without treating it as trusted data."""
    amount_token = r"(?:[¥￥]\s*)?[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
    match = re.search(
        rf"(?:退款|退(?!款))\s*(?P<amount>{amount_token})",
        message,
        re.IGNORECASE,
    )
    if match is None and re.fullmatch(
        rf"\s*{amount_token}(?:\s*(?:元|块|人民币))?\s*",
        message,
        re.IGNORECASE,
    ):
        match = re.search(amount_token, message)
    if match is None:
        malformed = re.search(
            r"(?:退款|退(?!款))\s*(?P<amount>[^\s,，]+)",
            message,
            re.IGNORECASE,
        )
        if malformed is not None:
            raise ValueError("INVALID_REFUND_AMOUNT_FORMAT")
        return None

    raw_amount = match.group("amount") if "amount" in match.groupdict() else match.group(0)
    raw_amount = re.sub(r"^[¥￥]\s*", "", str(raw_amount).strip())
    try:
        return Decimal(str(raw_amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("INVALID_REFUND_AMOUNT_FORMAT") from exc


def _is_full_refund_request(message: str) -> bool:
    return bool(re.search(r"全部退款|全额退款|退全部|全部退", message))


def _validate_refund_amount(
    amount: Decimal,
    refundable_amount: Decimal,
) -> str | None:
    if not amount.is_finite():
        return "退款金额格式不合法，请输入有效的数字。"
    if amount.as_tuple().exponent < -2:
        return "退款金额最多保留两位小数。"
    if amount <= 0:
        return "退款金额必须大于 0。"
    if amount > refundable_amount:
        return f"退款金额不能超过当前可退款金额 ¥{refundable_amount:.2f}。"
    return None

def refund_prepare_node(state: SupportState):
    """
    从用户消息中提取退款所需参数。

    这里只准备参数，
    不真正执行退款。
    """

    message = state["user_message"]

    guarded = _write_node_guard(state)
    if guarded:
        return guarded

    order_id = _target_order_id(state, message)

    if state.get("pending_action") == "refund" and re.search(
        r"取消|不要了|算了",
        message,
    ):
        return {
            "result": "已取消退款操作。",
            "error_log": "",
            "pending_action": "",
            "missing_fields": [],
            "refund_cancelled": True,
            "need_confirmation": False,
            "idempotency_key": "",
            "terminal_status": "CANCELLED",
            "execution_log": state["execution_log"]
            + ["refund_prepare_node"],
        }

    if not order_id:
        return {
            "result": "没有识别到订单号，请提供订单号。",
            "error_log": "",
            "pending_action": "refund",
            "missing_fields": ["order_id"],
            "terminal_status": "",
            "execution_log": state["execution_log"]
            + ["refund_prepare_node"],
        }

    order_lookup = dispatch_tool(
        "query_order",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=order_id,
    )
    if not order_lookup.success:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "pending_action": "refund",
            "missing_fields": ["order_id"],
            "result": order_lookup.error_message,
            "error_log": f"{order_lookup.error_code}: {order_lookup.error_message}",
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["refund_prepare_node"],
        }

    # These values are read from the trusted backend order tool, never from
    # the user message or an LLM extraction.
    order = order_lookup.data
    order_total = Decimal(str(order.get("amount", "0")))
    refundable_amount = Decimal(
        str(order.get("refundable_amount", order.get("amount", "0")))
    )

    if _is_full_refund_request(message):
        requested_amount = refundable_amount
    else:
        try:
            requested_amount = _extract_refund_amount(message)
        except ValueError:
            requested_amount = Decimal("-1")

    if requested_amount is None:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "order_total": float(order_total),
            "refundable_amount": float(refundable_amount),
            "refund_amount": 0.0,
            "pending_action": "refund",
            "missing_fields": ["amount"],
            "result": "请填写退款金额，可选择全部退款。",
            "error_log": "",
            "need_confirmation": False,
            "refund_cancelled": False,
            "execution_log": state["execution_log"]
            + ["refund_prepare_node"],
        }

    validation_error = _validate_refund_amount(
        requested_amount,
        refundable_amount,
    )
    if validation_error:
        return {
            "order_id": order_id,
            "current_order_id": order_id,
            "order_total": float(order_total),
            "refundable_amount": float(refundable_amount),
            "refund_amount": 0.0,
            "pending_action": "refund",
            "missing_fields": ["amount"],
            "result": validation_error,
            "error_log": f"INVALID_REFUND_AMOUNT: {validation_error}",
            "need_confirmation": False,
            "refund_cancelled": False,
            "terminal_status": "FAILED",
            "execution_log": state["execution_log"]
            + ["refund_prepare_node"],
        }

    # 为这次退款生成唯一 Idempotency Key
    idempotency_key = (
        str(state.get("idempotency_key") or "")
        or f"refund-{order_id}-{uuid.uuid4().hex}"
    )

    return {
        "order_id": order_id,
        "current_order_id": order_id,
        "order_total": float(order_total),
        "refundable_amount": float(refundable_amount),
        "refund_amount": float(requested_amount),
        "refund_reason": "用户申请退款",
        "idempotency_key": idempotency_key,
        "pending_action": "refund",
        "missing_fields": [],
        "refund_cancelled": False,
        "need_confirmation": True,
        "confirmed": False,
        "terminal_status": "",
        "result": "",
        "error_log": "",
        "execution_log": state["execution_log"]
        + ["refund_prepare_node"],
    }


# ============================================================
# 6. Refund Confirmation Node
# ============================================================

def refund_confirmation_node(state: SupportState):
    """
    敏感操作人工确认。

    interrupt() 会暂停当前 Graph，
    等待外部通过 Command(resume=...) 恢复。
    """

    confirmation = interrupt({
        "message": "退款属于敏感操作，需要用户确认",
        "order_id": state["order_id"],
        "amount": state["refund_amount"],
        "reason": state["refund_reason"],
    })

    confirmed = bool(confirmation)

    return {
        "confirmed": confirmed,
        "need_confirmation": False,
        "execution_log": state["execution_log"]
        + ["refund_confirmation_node"],
    }


# ============================================================
# 7. Refund Execute Node
# ============================================================

def refund_execute_node(state: SupportState):
    """
    用户确认以后执行退款。

    注意：
    即使用户已经确认，
    仍然必须经过 Dispatcher 的：
    Whitelist + Authorization。
    """

    if not state["confirmed"]:
        return {
            "result": "用户未确认退款，操作已取消。",
            "error_log": "",
            "need_confirmation": False,
            "pending_action": "",
            "missing_fields": [],
            "refund_cancelled": True,
            "idempotency_key": "",
            "terminal_status": "CANCELLED",
            "execution_log": state["execution_log"]
            + ["refund_execute_node"],
        }

    tool_result = dispatch_tool(
        "refund_order",
        role=state["role"],
        permissions=state.get("permissions"),
        order_id=state["order_id"],
        amount=state["refund_amount"],
        reason=state["refund_reason"],
        idempotency_key=state["idempotency_key"],
        confirmed=state["confirmed"],
    )

    if not tool_result.success:
        return {
            "result": tool_result.error_message,
            "error_log": (
                f"{tool_result.error_code}: "
                f"{tool_result.error_message}"
            ),
            "need_confirmation": False,
            "pending_action": "refund",
            "missing_fields": [],
            "terminal_status": (
                "PERMISSION_DENIED"
                if tool_result.error_code == "PERMISSION_DENIED"
                else "FAILED"
            ),
            "execution_log": state["execution_log"]
            + ["refund_execute_node"],
        }

    refund = tool_result.data

    return {
        "result": (
            f"订单 {refund['order_id']} 退款成功，"
            f"退款金额 ¥{refund['refund_amount']}。"
        ),
        "error_log": "",
        "need_confirmation": False,
        "pending_action": "",
        "missing_fields": [],
        "refund_cancelled": False,
        "idempotency_key": "",
        "terminal_status": "SUCCESS",
        "execution_log": state["execution_log"]
        + ["refund_execute_node"],
    }


# ============================================================
# 8. Clarification Node
# ============================================================

def clarification_node(state: SupportState):
    """Ask for an unambiguous target without performing any business action."""
    return {
        "result": state.get(
            "clarification_message",
            "请提供明确的订单号，我才能继续处理。",
        ),
        "error_log": "",
        "needs_clarification": True,
        "terminal_status": "NEED_USER_INPUT",
        "execution_log": state["execution_log"]
        + ["clarification_node"],
    }


# ============================================================
# 9. Fallback Node
# ============================================================

def fallback_node(state: SupportState):
    """
    无法识别的请求进入这里。
    """

    return {
        "result": (
            "抱歉，我目前只能处理客服知识、"
            "订单、物流和退款相关问题。"
        ),
        "error_log": "",
        "execution_log": state["execution_log"]
        + ["fallback_node"],
    }
