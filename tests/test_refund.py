from langgraph.types import Command

from app.graph import app
from app.state import create_initial_state


def test_manager_can_confirm_and_execute_refund():
    """
    manager 有 refund_order 权限。

    验证：
    refund prepare
    → interrupt
    → checkpoint
    → resume
    → authorization
    → refund success
    """

    state = create_initial_state(
        user_message="我要退款 100 元，订单 9527",
        role="manager",
    )

    config = {
        "configurable": {
            "thread_id": "test-manager-refund"
        }
    }

    # 第一次运行到人工确认处暂停。
    first_result = app.invoke(
        state,
        config=config,
    )

    assert first_result["intent"] == "refund"
    assert first_result["order_id"] == "9527"
    assert first_result["refund_amount"] == 100.0
    assert first_result["need_confirmation"] is True
    assert first_result["confirmed"] is False

    # Checkpoint 中应该存在尚未完成的节点。
    snapshot = app.get_state(config)

    assert snapshot.next
    assert "refund_confirmation" in snapshot.next

    # 模拟用户确认。
    final_result = app.invoke(
        Command(resume=True),
        config=config,
    )

    assert final_result["confirmed"] is True
    assert final_result["need_confirmation"] is False
    assert final_result["error_log"] == ""
    assert "退款成功" in final_result["result"]

    # Workflow 已完成。
    final_snapshot = app.get_state(config)

    assert not final_snapshot.next


def test_customer_service_cannot_execute_refund():
    """
    customer_service 即使获得用户确认，
    也不能绕过 Authorization 执行退款。
    """

    state = create_initial_state(
        user_message="我要退款 100 元，订单 9527",
        role="customer_service",
    )

    config = {
        "configurable": {
            "thread_id": "test-customer-service-refund"
        }
    }

    # V1.5g: authorization must fail before amount preparation or HITL.
    first_result = app.invoke(
        state,
        config=config,
    )

    assert first_result["need_confirmation"] is False
    assert first_result["pending_action"] == ""
    assert first_result["missing_fields"] == []
    assert "PERMISSION_DENIED" in first_result["error_log"]
    assert "无权调用工具 refund_order" in first_result["error_log"]
