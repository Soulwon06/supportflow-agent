from fastapi.testclient import TestClient

from app.api import api


client = TestClient(api)


def test_health():
    response = client.get("/health")

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "ok"
    assert data["service"] == "supportflow-agent"


def test_chat_order():
    response = client.post(
        "/chat",
        json={
            "message": "查询订单 9527",
            "thread_id": "test-api-order",
            "role": "customer_service",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["thread_id"] == "test-api-order"
    assert data["intent"] == "order"
    assert data["success"] is True
    assert data["result"]
    assert data["error_code"] is None