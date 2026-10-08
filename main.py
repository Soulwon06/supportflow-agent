from app.graph import app


def run_test(message: str):
    initial_state = {
    "user_message": message,
    "intent": "",
    "result": "",
    "error_log": "",
    "execution_log": [],

    "need_confirmation": False,
    "confirmed": False,
    "step_count": 0,
}

    result = app.invoke(initial_state)

    print(f"\n用户：{message}")
    print(f"Intent：{result['intent']}")
    print(f"Result：{result['result']}")
    print(f"Error Log：{result['error_log']}")
    print(f"Execution Log：{result['execution_log']}")


def main():
    test_messages = [
        "公司的退款期限是多少天？",
        "帮我查一下订单9527",
        "今天天气怎么样？",
    ]

    for message in test_messages:
        run_test(message)


if __name__ == "__main__":
    main()

