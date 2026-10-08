from app.graph import app


def make_state(message):
    return {
        "user_message": message,
        "intent": "",
        "result": "",
        "error_log": "",
        "execution_log": [],
        "need_confirmation": False,
        "confirmed": False,
        "step_count": 0,
        "order_id": "",
        "refund_amount": 0.0,
        "refund_reason": "",
        "idempotency_key": "",
    }


def test_case(message, thread_id):
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

    print(f"Result: {result['result']}")
    print(f"Error: {result['error_log']}")
    print(f"Execution Log: {result['execution_log']}")

    if result.get("__interrupt__"):
       print("Interrupt: YES")
    else:
       print("Interrupt: NO")


# 缺订单号
test_case(
    "给我退款500元",
    "refund-failure-001",
)

# 缺金额
test_case(
    "给订单9527退款",
    "refund-failure-002",
)

# 参数完整
test_case(
    "给订单9527退款500元",
    "refund-success-001",
)