from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.state import SupportState
from app.nodes import (
    refund_prepare_node,
    refund_confirmation_node,
    refund_execute_node,
)


# =========================
# 1. Build Graph
# =========================

builder = StateGraph(SupportState)

builder.add_node(
    "refund_prepare",
    refund_prepare_node,
)

builder.add_node(
    "refund_confirmation",
    refund_confirmation_node,
)

builder.add_node(
    "refund_execute",
    refund_execute_node,
)


# =========================
# 2. Connect Nodes
# =========================

builder.add_edge(
    START,
    "refund_prepare",
)

builder.add_edge(
    "refund_prepare",
    "refund_confirmation",
)

builder.add_edge(
    "refund_confirmation",
    "refund_execute",
)

builder.add_edge(
    "refund_execute",
    END,
)


# =========================
# 3. Checkpoint
# =========================

checkpointer = InMemorySaver()

graph = builder.compile(
    checkpointer=checkpointer,
)


# =========================
# 4. Thread
# =========================

config = {
    "configurable": {
        "thread_id": "refund-workflow-002"
    }
}


# =========================
# 5. Initial State
# =========================

initial_state = {
    "user_message": "给订单9527退款500元",
    "intent": "refund",
    "result": "",
    "error_log": "",
    "execution_log": [],

    "need_confirmation": False,
    "confirmed": False,

    "step_count": 0,

    # 这些现在不再人工填写
    # 后面由 refund_prepare_node 产生
    "order_id": "",
    "refund_amount": 0.0,
    "refund_reason": "",
    "idempotency_key": "",
}


# =========================
# 6. First Run
# =========================

print("========== 第一次运行 ==========")

result = graph.invoke(
    initial_state,
    config=config,
)

print(result)


# =========================
# 7. Resume
# =========================

print("\n========== 用户确认 ==========")

result = graph.invoke(
    Command(resume=True),
    config=config,
)

print(result)