from app.graph import app


# ============================================================
# 同一个 thread_id = 同一条会话状态链
# ============================================================

config = {
    "configurable": {
        "thread_id": "memory-demo-001",
    }
}


# ============================================================
# 第一轮：用户明确提供订单号 9527
# ============================================================

print("\n=== 第一轮：查询订单 ===")

first_state = {
    "user_message": "帮我查一下订单9527",
    "intent": "",
    "result": "",
    "error_log": "",
    "execution_log": [],
    "need_confirmation": False,
    "confirmed": False,
    "step_count": 0,
    "role": "customer_service",
    "order_id": "",
    "refund_amount": 0.0,
    "refund_reason": "",
    "idempotency_key": "",
}

result_1 = app.invoke(
    first_state,
    config=config,
)

print(f"Result: {result_1['result']}")
print(f"Order ID: {result_1['order_id']}")
print(f"Execution Log: {result_1['execution_log']}")


# ============================================================
# 第二轮：不再提供订单号
#
# 注意：
# 这里只更新 user_message。
#
# 不要重新传：
# order_id=""
#
# 否则会主动把上一轮保存的订单号覆盖掉。
# ============================================================

print("\n=== 第二轮：查询刚才那个订单的物流 ===")

result_2 = app.invoke(
    {
        "user_message": "刚才那个订单物流到哪里了？",
    },
    config=config,
)

print(f"Result: {result_2['result']}")
print(f"Order ID: {result_2['order_id']}")
print(f"Execution Log: {result_2['execution_log']}")