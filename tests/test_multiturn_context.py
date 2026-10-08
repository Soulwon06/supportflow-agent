from fastapi.testclient import TestClient
from langgraph.types import Command

from app.api import api
from app.graph import app
from app.state import create_initial_state, create_turn_input


def invoke_turn(message: str, thread_id: str, role: str = "customer_service"):
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = app.get_state(config)
    existing_state = snapshot.values if snapshot.values else None
    state = create_turn_input(
        user_message=message,
        role=role,
        existing_state=existing_state,
    )
    return app.invoke(state, config=config)


def test_order_then_pronoun_logistics_uses_checkpoint_order():
    thread_id = "multiturn-logistics-sf1001"
    first = app.invoke(
        create_initial_state("帮我查一下订单 SF1001"),
        config={"configurable": {"thread_id": thread_id}},
    )
    second = invoke_turn("它现在到哪了？", thread_id)

    assert first["intent"] == "order"
    assert first["current_order_id"] == "SF1001"
    assert first["order_history"] == ["SF1001"]
    assert first["step_count"] == 1
    assert second["intent"] == "logistics"
    assert second["current_order_id"] == "SF1001"
    assert "SF1001" in second["result"]
    assert second["step_count"] == 1


def test_context_refund_resolves_order_but_still_interrupts():
    thread_id = "multiturn-refund-sf1001"
    app.invoke(
        create_initial_state("查 SF1001"),
        config={"configurable": {"thread_id": thread_id}},
    )
    second = invoke_turn("退款 100 元", thread_id, role="manager")
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = app.get_state(config)

    assert second["intent"] == "refund"
    assert second["order_id"] == "SF1001"
    assert second["current_order_id"] == "SF1001"
    assert second["need_confirmation"] is True
    assert "refund_confirmation" in snapshot.next

    final = app.invoke(Command(resume=True), config=config)
    assert final["error_log"] == ""
    assert "退款成功" in final["result"]


def test_latest_explicit_order_becomes_current_focus():
    thread_id = "multiturn-switch-sf-orders"
    app.invoke(
        create_initial_state("查 SF1001"),
        config={"configurable": {"thread_id": thread_id}},
    )
    second = invoke_turn("再帮我查 SF2002", thread_id)
    third = invoke_turn("它现在到哪了？", thread_id)

    assert second["current_order_id"] == "SF2002"
    assert second["order_history"] == ["SF1001", "SF2002"]
    assert third["intent"] == "logistics"
    assert third["current_order_id"] == "SF2002"
    assert "SF2002" in third["result"]


def test_ordinal_reference_targets_first_order_and_keeps_hitl():
    thread_id = "multiturn-ordinal-refund"
    app.invoke(
        create_initial_state("查 SF1001"),
        config={"configurable": {"thread_id": thread_id}},
    )
    invoke_turn("查 SF2002", thread_id)
    third = invoke_turn("把第一个退款 100 元", thread_id, role="manager")

    assert third["intent"] == "refund"
    assert third["current_order_id"] == "SF1001"
    assert third["order_id"] == "SF1001"
    assert third["need_confirmation"] is True
    assert "refund_confirmation" in app.get_state(
        {"configurable": {"thread_id": thread_id}}
    ).next


def test_ambiguous_sensitive_reference_requests_clarification():
    thread_id = "multiturn-ambiguous-refund"
    app.invoke(
        create_initial_state("查 SF1001"),
        config={"configurable": {"thread_id": thread_id}},
    )
    invoke_turn("查 SF2002", thread_id)
    result = invoke_turn("退款 100 元", thread_id, role="manager")

    assert result["intent"] == "refund"
    assert result["needs_clarification"] is True
    assert result["need_confirmation"] is False
    assert "SF1001" in result["result"]
    assert "SF2002" in result["result"]
    assert "clarification_node" in result["execution_log"]
    assert not app.get_state(
        {"configurable": {"thread_id": thread_id}}
    ).next


def test_thread_checkpoint_context_is_isolated():
    thread_a = "multiturn-isolation-a"
    thread_b = "multiturn-isolation-b"
    app.invoke(
        create_initial_state("查 SF1001"),
        config={"configurable": {"thread_id": thread_a}},
    )
    app.invoke(
        create_initial_state("查 SF2002"),
        config={"configurable": {"thread_id": thread_b}},
    )

    result_a = invoke_turn("它现在到哪了？", thread_a)
    result_b = invoke_turn("它现在到哪了？", thread_b)

    assert result_a["current_order_id"] == "SF1001"
    assert "SF1001" in result_a["result"]
    assert result_b["current_order_id"] == "SF2002"
    assert "SF2002" in result_b["result"]


def test_api_preserves_context_between_turns():
    client = TestClient(api)
    thread_id = "multiturn-api-context"

    first = client.post(
        "/chat",
        json={"message": "查 SF1001", "thread_id": thread_id},
    )
    second = client.post(
        "/chat",
        json={"message": "它现在到哪了？", "thread_id": thread_id},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["intent"] == "logistics"
    assert second.json()["routing_source"] == "llm"
    assert "SF1001" in second.json()["result"]
