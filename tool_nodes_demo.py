from app.nodes import (
    order_node,
    logistics_node,
)


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


print("========== Order Node ==========")

order_state = make_state(
    "帮我查一下订单9527"
)

order_result = order_node(order_state)

print(order_result)


print("\n========== Logistics Node ==========")

logistics_state = make_state(
    "订单9527的物流到哪里了？"
)

logistics_result = logistics_node(
    logistics_state
)

print(logistics_result)