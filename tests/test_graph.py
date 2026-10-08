from app.graph import app
from app.state import create_initial_state


def test_order_flow():
    state = create_initial_state(
        user_message="查询订单 9527",
        role="customer_service",
    )

    config = {
        "configurable": {
            "thread_id": "test-order-flow"
        }
    }

    result = app.invoke(
        state,
        config=config,
    )

    assert result["intent"] == "order"
    assert result["result"]
    assert result["error_log"] == ""