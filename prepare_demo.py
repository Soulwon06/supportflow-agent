from app.nodes import refund_prepare_node


state = {
    "user_message": "给订单9527退款500元",
    "intent": "refund",
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


result = refund_prepare_node(state)

print("=== Refund Prepare Result ===")
print(result)