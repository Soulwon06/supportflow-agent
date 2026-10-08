from app.nodes import router_node
from app.state import create_initial_state


def test_router_order():
    state = create_initial_state(
        user_message="查询订单 9527",
        role="customer_service",
    )

    result = router_node(state)

    assert result["intent"] == "order"


def test_router_logistics():
    state = create_initial_state(
        user_message="订单 9527 的物流到哪里了？",
        role="customer_service",
    )

    result = router_node(state)

    assert result["intent"] == "logistics"


def test_router_refund():
    state = create_initial_state(
        user_message="我要退款订单 9527",
        role="customer_service",
    )

    result = router_node(state)

    assert result["intent"] == "refund"