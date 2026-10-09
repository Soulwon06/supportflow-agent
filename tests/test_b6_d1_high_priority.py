import json

from fastapi.testclient import TestClient

from app import api as api_module
from app.auth import create_user
from app.database import connection, initialize_database
from app.graph import app
from app.nodes import outbound_submit_node
from app.state import create_initial_state, create_turn_input


def _state(message, *, role, user_id, permissions):
    state = create_initial_state(message, role=role)
    state["user_id"] = user_id
    state["permissions"] = list(permissions)
    return state


def test_high_risk_negations_block_all_write_flows(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "negations.sqlite"))
    initialize_database()
    employee = create_user("negation-employee", "Negation-2026!", "测试员工", "生产部")
    with connection() as conn:
        initial_order_count = conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0]
    cases = [
        (
            "不要提交出库 SF2002 10 个",
            "warehouse_employee",
            ["query_order", "query_inventory", "request_outbound"],
        ),
        (
            "先别报工 SF2002 完成 10 个",
            "production_employee",
            ["query_order", "query_production", "submit_production_report"],
        ),
        (
            "暂不创建订单 CUST-001 下单 REEL-7IN-BLACK 10个，交期 2026-09-30",
            "manager",
            [],
        ),
    ]
    for index, (message, role, permissions) in enumerate(cases):
        result = app.invoke(
            _state(message, role=role, user_id=employee["id"], permissions=permissions),
            config={"configurable": {"thread_id": f"d1-negation-{index}"}},
        )
        assert result["write_guarded"] is True
        assert result["terminal_status"] == "NEED_USER_INPUT"
        assert "未创建申请" in result["result"]
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sales_orders").fetchone()[0] == initial_order_count


def test_negation_guard_distinguishes_double_negation_query_and_condition(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "negation-distinction.sqlite"))
    initialize_database()
    employee = create_user("negation-distinction", "Negation-2026!", "测试员工", "仓储部")
    common = ["query_order", "query_inventory", "request_outbound"]

    double_negative = app.invoke(
        _state("不要忘记提交出库 SF2002 10 个", role="warehouse_employee", user_id=employee["id"], permissions=common),
        config={"configurable": {"thread_id": "d1-double-negative"}},
    )
    assert double_negative["write_guarded"] is False
    assert double_negative["intent"] == "outbound_submit"

    query = app.invoke(
        _state("为什么不能提交出库", role="warehouse_employee", user_id=employee["id"], permissions=common),
        config={"configurable": {"thread_id": "d1-negation-query"}},
    )
    assert query["write_guarded"] is True
    assert "不会提交" in query["result"]

    conditional = app.invoke(
        _state("如果库存不足就不要提交出库 SF2002 10 个", role="warehouse_employee", user_id=employee["id"], permissions=common),
        config={"configurable": {"thread_id": "d1-conditional"}},
    )
    assert conditional["write_guarded"] is True
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0


def test_write_node_rechecks_negation_when_provider_misclassifies(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "node-guard.sqlite"))
    initialize_database()
    employee = create_user("node-guard", "NodeGuard-2026!", "测试员工", "仓储部")
    state = _state(
        "不要提交出库 SF2002 10 个",
        role="warehouse_employee",
        user_id=employee["id"],
        permissions=["query_order", "query_inventory", "request_outbound"],
    )
    state["intent"] = "outbound_submit"
    result = outbound_submit_node(state)
    assert result["write_guarded"] is True
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0


def test_employee_natural_language_order_creates_pending_request_not_order(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "employee-order.sqlite"))
    initialize_database()
    employee = create_user("sales-natural", "SalesNatural-2026!", "业务员", "业务部")
    permissions = ["query_order", "query_logistics", "submit_order"]
    result = app.invoke(
        _state(
            "CUST-001 下单 REEL-7IN-BLACK 500个，交期 2026-09-30",
            role="sales_employee",
            user_id=employee["id"],
            permissions=permissions,
        ),
        config={"configurable": {"thread_id": "d1-employee-order"}},
    )
    assert result["intent"] == "order_entry"
    assert result["terminal_status"] == "PENDING_APPROVAL"
    assert "等待管理员审批" in result["result"]
    with connection() as conn:
        request = conn.execute(
            "SELECT request_type, status, payload_json FROM operation_requests"
        ).fetchone()
        assert request["request_type"] == "order_submission"
        assert request["status"] == "SUBMITTED"
        assert json.loads(request["payload_json"])["quantity"] == 500
        assert conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE order_no LIKE 'SF-NEW-%'"
        ).fetchone()[0] == 0


def test_employee_order_missing_fields_enters_supplement_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "employee-order-slots.sqlite"))
    initialize_database()
    employee = create_user("sales-slots", "SalesSlots-2026!", "业务员", "业务部")
    permissions = ["query_order", "query_logistics", "submit_order"]
    first = app.invoke(
        _state("CUST-001 下单 REEL-7IN-BLACK 500个", role="sales_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d1-order-slots"}},
    )
    assert first["pending_action"] == "order_entry"
    assert "required_date" in first["missing_fields"]
    second = app.invoke(
        create_turn_input(
            "交期 2026-09-30",
            role="sales_employee",
            user_id=employee["id"],
            existing_state=first,
        ),
        config={"configurable": {"thread_id": "d1-order-slots"}},
    )
    second["permissions"] = permissions
    # The second invocation above used a full state input, so it is already
    # the graph result; the assertion below verifies no accidental direct order.
    assert second["terminal_status"] == "PENDING_APPROVAL"


def test_quality_submission_is_separate_from_quality_query(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "quality-submit.sqlite"))
    initialize_database()
    employee = create_user("quality-natural", "QualityNatural-2026!", "质检员", "质检部")
    permissions = ["query_order", "query_production", "submit_quality_report"]
    before = app.invoke(
        _state("SF1001 质检结果是什么", role="quality_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d1-quality-query"}},
    )
    assert before["intent"] == "quality"
    assert before["terminal_status"] == "SUCCESS"
    with connection() as conn:
        before_count = conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0]

    submitted = app.invoke(
        _state(
            "提交质检 SF1001 检验100合格90不合格10",
            role="quality_employee",
            user_id=employee["id"],
            permissions=permissions,
        ),
        config={"configurable": {"thread_id": "d1-quality-submit"}},
    )
    assert submitted["intent"] == "quality_submit"
    assert submitted["terminal_status"] == "PENDING_APPROVAL"
    with connection() as conn:
        request = conn.execute(
            "SELECT request_type, status, payload_json FROM operation_requests ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == before_count + 1
        assert request["request_type"] == "quality_report"
        assert request["status"] == "SUBMITTED"
        payload = json.loads(request["payload_json"])
        assert payload["inspected_quantity"] == 100
        assert payload["qualified_quantity"] == 90
        assert payload["rejected_quantity"] == 10


def test_quality_submission_missing_or_unauthorized_is_safe(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "quality-submit-safety.sqlite"))
    initialize_database()
    employee = create_user("quality-safety", "QualitySafety-2026!", "普通员工", "生产部")
    missing = app.invoke(
        _state("提交质检 SF1001", role="quality_employee", user_id=employee["id"], permissions=["submit_quality_report"]),
        config={"configurable": {"thread_id": "d1-quality-missing"}},
    )
    assert missing["terminal_status"] == "NEED_USER_INPUT"
    assert "检验数量" in missing["result"]
    denied = app.invoke(
        _state("提交质检 SF1001 检验100合格90不合格10", role="employee", user_id=employee["id"], permissions=["query_order"]),
        config={"configurable": {"thread_id": "d1-quality-denied"}},
    )
    assert denied["terminal_status"] == "PERMISSION_DENIED"
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0


def test_quality_submit_chat_and_stream_routes(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "quality-submit-api.sqlite"))
    initialize_database()
    employee = create_user("quality-api", "QualityApi-2026!", "质检员", "质检部")
    identity = {
        "id": employee["id"],
        "role": "quality_employee",
        "permissions": ["query_order", "query_production", "submit_quality_report"],
    }
    monkeypatch.setattr(api_module, "_identity", lambda request, requested_role=None: identity)
    client = TestClient(api_module.api)
    payload = {
        "message": "提交质检 SF1001 检验100合格90不合格10",
        "thread_id": "d1-quality-api",
        "role": "quality_employee",
    }
    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert response.json()["intent"] == "quality_submit"
    stream = client.post("/chat/stream", json={**payload, "thread_id": "d1-quality-stream"})
    assert stream.status_code == 200
    assert '"intent": "quality_submit"' in stream.text
