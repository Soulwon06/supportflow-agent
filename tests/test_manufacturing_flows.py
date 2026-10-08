from fastapi.testclient import TestClient

from app.api import api
from app.auth import create_user
from app.database import initialize_database
from app.graph import app
from app.state import create_initial_state, create_turn_input


client = TestClient(api)


def test_inventory_question_uses_inventory_intent_and_database(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "inventory.sqlite"))
    initialize_database()

    response = client.post(
        "/chat",
        json={
            "message": "REEL-7IN-BLACK 还有多少库存？",
            "thread_id": "manufacturing-inventory",
            "role": "customer_service",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "inventory"
    assert "可用" in data["result"]
    assert data["terminal_status"] == "SUCCESS"


def test_production_question_reports_completed_and_qualified_quantity(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "production.sqlite"))
    initialize_database()

    response = client.post(
        "/chat",
        json={
            "message": "SF2002 已完成多少个，合格多少个？",
            "thread_id": "manufacturing-production",
            "role": "customer_service",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "production"
    assert "已报工 320 个" in data["result"]
    assert "合格 315 个" in data["result"]


def test_manager_can_submit_synthetic_sales_order(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "order-entry.sqlite"))
    initialize_database()

    response = client.post(
        "/chat",
        json={
            "message": "CUST-001 下单 REEL-7IN-BLACK 500个，交期 2026-09-30",
            "thread_id": "manufacturing-order-entry",
            "role": "manager",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "order_entry"
    assert "上单已提交" in data["result"]
    assert data["terminal_status"] == "SUCCESS"


def test_quality_query_is_distinct_from_production_query(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "quality-query.sqlite"))
    initialize_database()
    response = client.post(
        "/chat",
        json={"message": "SF1001 质检结果怎么样？", "thread_id": "quality-query"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "quality"
    assert "质检结果查询" in data["result"]


def test_explicit_order_progress_wording_routes_to_order(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "order-progress.sqlite"))
    initialize_database()
    response = client.post(
        "/chat",
        json={
            "message": "SF2002订单需要多少个，已经完成多少个？",
            "thread_id": "order-progress-query",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "order"
    assert "SF2002" in data["result"]
    assert "需求 500 个" in data["result"]
    assert "已完成 320 个" in data["result"]
    assert "剩余 180 个" in data["result"]


def test_quality_summary_wording_routes_to_quality(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "quality-summary.sqlite"))
    initialize_database()
    response = client.post(
        "/chat",
        json={
            "message": "SF1001合格多少个，不合格多少个？",
            "thread_id": "quality-summary-query",
        },
    )
    assert response.status_code == 200
    assert response.json()["intent"] == "quality"


def test_missing_production_order_requests_clarification(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "production-clarification.sqlite"))
    initialize_database()
    response = client.post(
        "/chat",
        json={
            "message": "生产进度怎么样？",
            "thread_id": "production-clarification",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["terminal_status"] == "NEED_USER_INPUT"
    assert "订单号" in data["result"]


def test_natural_language_outbound_is_submission(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "outbound-language.sqlite"))
    initialize_database()
    employee = create_user("outbound_operator", "Outbound-2026!", "仓库员工", "仓储部")
    permissions = ["query_order", "query_inventory", "query_logistics", "request_outbound"]
    state = create_initial_state("SF5005出库20个", role="warehouse_employee")
    state["permissions"] = permissions
    state["user_id"] = employee["id"]
    result = app.invoke(
        state,
        config={"configurable": {"thread_id": "outbound-language"}},
    )
    assert result["intent"] == "outbound_submit"
    assert result["terminal_status"] == "PENDING_APPROVAL"
    assert "等待管理员审批" in result["result"]


def test_natural_language_production_submission_supports_follow_up_slots(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "natural-submit.sqlite"))
    initialize_database()
    employee = create_user("natural_operator", "Natural-2026!", "自然语言生产员工", "生产部")
    permissions = ["query_order", "query_production", "submit_production_report"]
    config = {"configurable": {"thread_id": "natural-production-submit"}}
    first_state = create_initial_state("我要提交生产报工", role="production_employee")
    first_state["permissions"] = permissions
    first_state["user_id"] = employee["id"]
    first = app.invoke(first_state, config=config)
    assert first["intent"] == "production_submit"
    assert first["pending_action"] == "production_submit"
    assert "订单号" in first["result"]

    second_state = create_turn_input("SF2002 完成 10 个", role="production_employee", user_id=employee["id"], existing_state=first)
    second_state["permissions"] = permissions
    second = app.invoke(second_state, config=config)
    assert second["pending_action"] == ""
    assert second["terminal_status"] == "PENDING_APPROVAL"
    assert "生产报工申请已提交" in second["result"]
