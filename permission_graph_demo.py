from langgraph.types import Command

from app.graph import app


def make_state(
    message,
    role,
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


def test_refund(
    role,
    thread_id,
):
    print("\n" + "=" * 60)
    print(f"当前角色：{role}")
    print("用户：给订单9527退款500元")

    config = {
        "configurable": {
            "thread_id": thread_id,
        }
    }

    # 第一次执行：应该暂停在人工确认
    result = app.invoke(
        make_state(
            "给订单9527退款500元",
            role=role,
        ),
        config=config,
    )

    print("\n--- Interrupt 后 ---")
    print(f"Role: {result['role']}")
    print(f"Execution Log: {result['execution_log']}")
    print(
        "Interrupt:",
        "YES"
        if result.get("__interrupt__")
        else "NO",
    )

    # 模拟用户确认退款
    result = app.invoke(
        Command(resume=True),
        config=config,
    )

    print("\n--- 用户确认并 Resume 后 ---")
    print(f"Role: {result['role']}")
    print(f"Result: {result['result']}")
    print(f"Error: {result['error_log']}")
    print(f"Execution Log: {result['execution_log']}")


# Case 1：普通客服
test_refund(
    role="customer_service",
    thread_id="permission-customer-service-001",
)


# Case 2：主管
test_refund(
    role="manager",
    thread_id="permission-manager-001",
)