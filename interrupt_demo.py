from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import interrupt, Command


class DemoState(TypedDict):
    order_id: str
    amount: float
    confirmed: bool


def refund_confirmation_node(state: DemoState):
    print("进入 refund_confirmation_node")

    answer = interrupt(
        {
            "message": "退款属于敏感操作，需要用户确认",
            "order_id": state["order_id"],
            "amount": state["amount"],
        }
    )

    print(f"收到 Resume 数据：{answer}")

    return {
        "confirmed": bool(answer),
    }


builder = StateGraph(DemoState)

builder.add_node(
    "refund_confirmation",
    refund_confirmation_node,
)

builder.add_edge(START, "refund_confirmation")
builder.add_edge("refund_confirmation", END)


# Checkpointer：保存 Graph State
checkpointer = InMemorySaver()

graph = builder.compile(
    checkpointer=checkpointer,
)


# 同一个 thread_id 代表同一条执行线程
config = {
    "configurable": {
        "thread_id": "refund-demo-001"
    }
}


print("========== 第一次运行 ==========")

result = graph.invoke(
    {
        "order_id": "9527",
        "amount": 500.0,
        "confirmed": False,
    },
    config=config,
)

print(result)


print("\n========== 用户确认后 Resume ==========")

result = graph.invoke(
    Command(resume=True),
    config=config,
)

print(result)