import json

from fastapi.testclient import TestClient
from langgraph.types import Command

from app.api import api
from app.graph import app
from app.state import create_initial_state


client = TestClient(api)


def test_workbench_and_trace_payload_are_served_without_secrets():
    index = client.get("/")
    assert index.status_code == 200
    assert "SupportFlow Workbench" in index.text

    response = client.post(
        "/chat",
        json={
            "message": "商品有质量问题应该怎么处理？",
            "thread_id": "workbench-trace-test",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["trace"]["intent"] == "knowledge"
    assert payload["trace"]["evidence"]["id"] == "quality_policy_01"
    assert payload["trace"]["answerability_status"] == "SUPPORTED"
    assert payload["trace"]["generation_skipped"] is False
    assert "api_key" not in json.dumps(payload).lower()
    assert "authorization" not in json.dumps(payload).lower()


def test_stream_exposes_runtime_trace_event():
    with client.stream(
        "POST",
        "/chat/stream",
        json={
            "message": "商品有质量问题应该怎么处理？",
            "thread_id": "workbench-stream-test",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: node" in body
    assert "event: progress" in body
    assert "正在理解你的问题" in body
    assert "正在确认资料是否足够回答" in body
    assert "event: rag" in body
    assert "event: trace" in body
    assert "quality_policy_01" in body
    assert "api_key" not in body.lower()


def test_stream_marks_missing_order_as_user_input_not_success():
    with client.stream(
        "POST",
        "/chat/stream",
        json={
            "message": "生产进度怎么样？",
            "thread_id": "workbench-stream-needs-input",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert 'event: terminal' in body
    assert '"status": "NEED_USER_INPUT"' in body
    assert "请提供订单号" in body


def test_resume_endpoint_uses_backend_checkpoint_and_permission():
    manager_thread = "workbench-manager-resume"
    first = client.post(
        "/chat",
        json={
            "message": "我要退款 100 元，订单 9527",
            "thread_id": manager_thread,
            "role": "manager",
        },
    )
    assert first.status_code == 200
    assert first.json()["success"] is True

    resumed = client.post(
        "/chat/resume",
        json={"thread_id": manager_thread, "confirmed": True},
    )
    assert resumed.status_code == 200
    assert "退款成功" in resumed.json()["result"]

    customer_thread = "workbench-customer-resume"
    denied_first = client.post(
        "/chat",
        json={
            "message": "我要退款 100 元，订单 9527",
            "thread_id": customer_thread,
            "role": "customer_service",
        },
    )
    assert denied_first.status_code == 200
    assert denied_first.json()["success"] is False
    assert denied_first.json()["error_code"] == "AGENT_ERROR"
    assert denied_first.json()["interaction"] is None

    denied = client.post(
        "/chat/resume",
        json={"thread_id": customer_thread, "confirmed": True},
    )
    assert denied.status_code == 409


def test_refund_stream_emits_backend_hitl_event():
    with client.stream(
        "POST",
        "/chat/stream",
        json={
            "message": "我要退款 100 元，订单 9527",
            "thread_id": "workbench-stream-hitl",
            "role": "manager",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "event: hitl" in body
    assert "需要用户确认" in body
