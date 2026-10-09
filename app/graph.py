from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver

from .state import SupportState
from .instrumentation import traced_node
from .nodes import (
    router_node,
    knowledge_node,
    order_node,
    logistics_node,
    inventory_node,
    production_node,
    quality_node,
    production_submit_node,
    outbound_submit_node,
    order_entry_node,
    fallback_node,
    refund_prepare_node,
    refund_confirmation_node,
    refund_execute_node,
    clarification_node,
)


# ============================================================
# Conditional Edge
# ============================================================

def choose_route(state: SupportState):
    """
    根据 router_node 写入 State 的 intent，
    决定下一步进入哪个业务节点。
    """

    intent = state["intent"]

    if state.get("error_log"):
        return "stop"

    if state.get("needs_clarification"):
        return "clarification"

    if intent in {"knowledge", "manufacturing_knowledge"}:
        return "knowledge"

    elif intent == "order":
        return "order"

    elif intent == "logistics":
        return "logistics"

    elif intent == "inventory":
        return "inventory"

    elif intent == "production":
        return "production"
    elif intent == "quality":
        return "quality"

    elif intent == "production_submit":
        return "production_submit"

    elif intent == "outbound_submit":
        return "outbound_submit"

    elif intent == "order_entry":
        return "order_entry"

    elif intent == "refund":
        return "refund"

    else:
        return "fallback"


def choose_refund_prepare_route(state: SupportState):
    """
    判断退款参数准备是否成功。

    如果 error_log 不为空：
        参数准备失败 → 结束 Workflow

    如果 error_log 为空：
        参数准备成功 → 进入人工确认
    """

    if state["error_log"] or state.get("refund_cancelled"):
        return "stop"

    if state.get("missing_fields") or not state.get("need_confirmation"):
        return "stop"

    return "continue"


# ============================================================
# Build Graph
# ============================================================

builder = StateGraph(SupportState)


# ============================================================
# Add Nodes
# ============================================================

builder.add_node(
    "router",
    traced_node(
        "router",
        router_node,
    ),
)

builder.add_node(
    "knowledge",
    traced_node(
        "knowledge",
        knowledge_node,
    ),
)

builder.add_node(
    "order",
    traced_node(
        "order",
        order_node,
    ),
)

builder.add_node(
    "logistics",
    traced_node(
        "logistics",
        logistics_node,
    ),
)

builder.add_node(
    "inventory",
    traced_node(
        "inventory",
        inventory_node,
    ),
)

builder.add_node(
    "production",
    traced_node(
        "production",
        production_node,
    ),
)

builder.add_node("quality", traced_node("quality", quality_node))

builder.add_node("production_submit", traced_node("production_submit", production_submit_node))
builder.add_node("outbound_submit", traced_node("outbound_submit", outbound_submit_node))

builder.add_node(
    "order_entry",
    traced_node(
        "order_entry",
        order_entry_node,
    ),
)

builder.add_node(
    "fallback",
    traced_node(
        "fallback",
        fallback_node,
    ),
)

builder.add_node(
    "refund_prepare",
    traced_node(
        "refund_prepare",
        refund_prepare_node,
    ),
)

builder.add_node(
    "refund_confirmation",
    traced_node(
        "refund_confirmation",
        refund_confirmation_node,
    ),
)

builder.add_node(
    "refund_execute",
    traced_node(
        "refund_execute",
        refund_execute_node,
    ),
)

builder.add_node(
    "clarification",
    traced_node(
        "clarification",
        clarification_node,
    ),
)


# ============================================================
# START → Router
# ============================================================

builder.add_edge(
    START,
    "router",
)


# ============================================================
# Router → Conditional Routes
# ============================================================

builder.add_conditional_edges(
    "router",
    choose_route,
    {
        "knowledge": "knowledge",
        "order": "order",
        "logistics": "logistics",
        "inventory": "inventory",
        "production": "production",
        "quality": "quality",
        "production_submit": "production_submit",
        "outbound_submit": "outbound_submit",
        "order_entry": "order_entry",

        # choose_route 返回 "refund"
        # 但真正进入的是 refund_prepare
        "refund": "refund_prepare",

        "clarification": "clarification",

        "fallback": "fallback",
        "stop": END,
    },
)


# ============================================================
# Normal Routes → END
# ============================================================

builder.add_edge(
    "knowledge",
    END,
)

builder.add_edge(
    "order",
    END,
)

builder.add_edge(
    "logistics",
    END,
)

builder.add_edge(
    "inventory",
    END,
)

builder.add_edge(
    "production",
    END,
)

builder.add_edge("quality", END)
builder.add_edge("production_submit", END)
builder.add_edge("outbound_submit", END)

builder.add_edge(
    "order_entry",
    END,
)

builder.add_edge(
    "fallback",
    END,
)

builder.add_edge(
    "clarification",
    END,
)


# ============================================================
# Refund Workflow
# ============================================================

builder.add_conditional_edges(
    "refund_prepare",
    choose_refund_prepare_route,
    {
        "continue": "refund_confirmation",
        "stop": END,
    },
)

builder.add_edge(
    "refund_confirmation",
    "refund_execute",
)

builder.add_edge(
    "refund_execute",
    END,
)


# ============================================================
# Checkpointer
# ============================================================

checkpointer = InMemorySaver()


# ============================================================
# Compile Graph
# ============================================================

app = builder.compile(
    checkpointer=checkpointer,
)
