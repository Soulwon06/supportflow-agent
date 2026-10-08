from app.nodes import router_node


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


test_messages = [
    "公司的退款期限是多少天？",
    "给订单9527退款500元",
    "帮我查一下订单9527",
    "订单9527的物流到哪里了？",
    "今天天气怎么样？",
]


for message in test_messages:
    state = make_state(message)
    result = router_node(state)

    print(
        f"{message} -> {result['intent']}"
    )