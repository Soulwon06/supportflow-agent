from langgraph.types import Command

from app.graph import app


def make_state(
    message,
    role="customer_service",
):
    return {
        "user_message": message,
        "intent": "",
        "result": "",
        "error_log": "",
        "execution_log": [],
        "need_confirmation": False,
        "confirmed": False,
        "step_count": 0,
        "role": role,
        "order_id": "",
        "refund_amount": 0.0,
        "refund_reason": "",
        "idempotency_key": "",
    }

def run_normal_case(message, thread_id):
    print("\n" + "=" * 60)
    print(f"用户：{message}")

    config = {
        "configurable": {
            "thread_id": thread_id
        }
    }

    result = app.invoke(
        make_state(message),
        config=config,
    )

    print(f"Intent: {result['intent']}")
    print(f"Result: {result['result']}")
    print(f"Error: {result['error_log']}")
    print(f"Execution Log: {result['execution_log']}")


# ============================================================
# 1. Knowledge
# ============================================================

run_normal_case(
    "公司的退款期限是多少天？",
    "demo-knowledge-001",
)


# ============================================================
# 2. Order
# ============================================================

run_normal_case(
    "帮我查一下订单9527",
    "demo-order-001",
)


# ============================================================
# 3. Logistics
# ============================================================

run_normal_case(
    "订单9527的物流到哪里了？",
    "demo-logistics-001",
)


# ============================================================
# 4. Fallback
# ============================================================

run_normal_case(
    "今天天气怎么样？",
    "demo-fallback-001",
)


# ============================================================
# 5. Refund
# ============================================================

print("\n" + "=" * 60)
print("用户：给订单9527退款500元")

refund_config = {
    "configurable": {
        "thread_id": "demo-refund-001"
    }
}

refund_result = app.invoke(
    make_state("给订单9527退款500元"),
    config=refund_config,
)

print("\n--- Interrupt 后 ---")
print(refund_result)


print("\n用户确认退款：True")

refund_result = app.invoke(
    Command(resume=True),
    config=refund_config,
)

print("\n--- Resume 后 ---")
print(refund_result)