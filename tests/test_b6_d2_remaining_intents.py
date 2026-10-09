import json

from fastapi.testclient import TestClient

from app import api as api_module
from app.auth import create_user, ensure_bootstrap_admin
from app.database import connection, initialize_database
from app.graph import app
from app.operations import create_request, decide_request
from app.state import create_initial_state


def _state(message, *, role="warehouse_employee", user_id=0, permissions=None):
    state = create_initial_state(message, role=role)
    state["user_id"] = user_id
    state["permissions"] = list(permissions or [])
    return state


def test_chat_approval_language_is_guarded_without_side_effect(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "approval-language.sqlite"))
    initialize_database()
    messages = [
        "批准出库申请 123",
        "拒绝订单申请 123",
        "审批生产报工申请 123",
        "同意这条申请",
        "同意",
    ]
    for index, message in enumerate(messages):
        result = app.invoke(
            _state(message, role="employee", permissions=["request_outbound"]),
            config={"configurable": {"thread_id": f"d2-approval-{index}"}},
        )
        assert result["intent"] == "admin_approval"
        assert result["terminal_status"] == "NEED_USER_INPUT"
        assert result["write_guarded"] is True
        assert "管理员审批看板" in result["result"]
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0] == 0


def test_outbound_status_is_read_only_and_distinct_from_logistics(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "outbound-status.sqlite"))
    initialize_database()
    permissions = ["query_order", "query_logistics", "request_outbound"]
    with connection() as conn:
        conn.execute(
            "UPDATE sales_order_lines SET shipped_quantity = 0 WHERE order_id = (SELECT id FROM sales_orders WHERE order_no = 'SF1001')"
        )

    initial = app.invoke(
        _state("SF1001 出库了吗？", permissions=permissions),
        config={"configurable": {"thread_id": "d2-outbound-status-initial"}},
    )
    assert initial["intent"] == "outbound_status"
    assert "尚未出库" in initial["result"]
    assert "已出库 0 个" in initial["result"]

    with connection() as conn:
        conn.execute(
            "UPDATE sales_order_lines SET shipped_quantity = 200 WHERE order_id = (SELECT id FROM sales_orders WHERE order_no = 'SF1001')"
        )
    partial = app.invoke(
        _state("SF1001 已经发了多少件？", permissions=permissions),
        config={"configurable": {"thread_id": "d2-outbound-status-partial"}},
    )
    assert partial["intent"] == "outbound_status"
    assert "部分出库" in partial["result"]
    assert "剩余未出库 800 个" in partial["result"]

    with connection() as conn:
        conn.execute(
            "UPDATE sales_order_lines SET shipped_quantity = 1000 WHERE order_id = (SELECT id FROM sales_orders WHERE order_no = 'SF1001')"
        )
    complete = app.invoke(
        _state("查看 SF1001 的出库状态", permissions=permissions),
        config={"configurable": {"thread_id": "d2-outbound-status-complete"}},
    )
    assert complete["intent"] == "outbound_status"
    assert "已全部出库" in complete["result"]
    assert "剩余未出库 0 个" in complete["result"]

    missing = app.invoke(
        _state("SF9999 出库了吗？", permissions=permissions),
        config={"configurable": {"thread_id": "d2-outbound-status-missing"}},
    )
    assert missing["terminal_status"] == "FAILED"
    assert "未找到订单 SF9999" in missing["result"]

    logistics = app.invoke(
        _state("SF1001 物流到哪了？", permissions=permissions),
        config={"configurable": {"thread_id": "d2-logistics-distinct"}},
    )
    assert logistics["intent"] == "logistics"
    with connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM operation_requests").fetchone()[0] == 0


def test_outbound_status_chat_and_stream_are_compatible(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "outbound-status-api.sqlite"))
    initialize_database()
    employee = create_user("status-api", "StatusApi-2026!", "仓库员工", "仓储部")
    identity = {
        "id": employee["id"],
        "role": "warehouse_employee",
        "permissions": ["query_order", "query_inventory", "query_logistics", "request_outbound"],
    }
    monkeypatch.setattr(api_module, "_identity", lambda request, requested_role=None: identity)
    client = TestClient(api_module.api)
    payload = {
        "message": "SF1001 出库了吗？",
        "thread_id": "d2-status-api",
        "role": "warehouse_employee",
    }
    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    assert response.json()["intent"] == "outbound_status"
    stream = client.post("/chat/stream", json={**payload, "thread_id": "d2-status-stream"})
    assert stream.status_code == 200
    assert '"intent": "outbound_status"' in stream.text


def test_pending_production_duplicate_uses_business_payload_only(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "production-duplicates.sqlite"))
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_USERNAME", "d2-admin")
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_PASSWORD", "D2Admin-2026!")
    initialize_database()
    ensure_bootstrap_admin()
    employee = create_user("d2-production", "D2Production-2026!", "生产员工", "生产部")
    other_employee = create_user("d2-production-2", "D2Production-2026!", "另一生产员工", "生产部")
    permissions = ["query_order", "query_production", "submit_production_report"]

    first = app.invoke(
        _state("SF2002 完成 10 个", role="production_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d2-production-first"}},
    )
    assert first["terminal_status"] == "PENDING_APPROVAL"
    with connection() as conn:
        first_request = conn.execute(
            "SELECT id FROM operation_requests WHERE request_type = 'production_report' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]

    duplicate = app.invoke(
        _state("SF2002 完成 10 个", role="production_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d2-production-duplicate"}},
    )
    with connection() as conn:
        same_pending = conn.execute(
            "SELECT id FROM operation_requests WHERE request_type = 'production_report' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        assert same_pending == first_request
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_requests WHERE request_type = 'production_report'"
        ).fetchone()[0] == 1

    with connection() as conn:
        admin_id = conn.execute(
            "SELECT id FROM users WHERE username = 'd2-admin'"
        ).fetchone()[0]
    assert decide_request(first_request, admin_id, True, "审批通过")["status"] == "APPROVED"

    after_approval = app.invoke(
        _state("SF2002 完成 10 个", role="production_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d2-production-after-approval"}},
    )
    assert after_approval["terminal_status"] == "PENDING_APPROVAL"

    other_user = app.invoke(
        _state("SF2002 完成 10 个", role="production_employee", user_id=other_employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d2-production-other-user"}},
    )
    different_payload = app.invoke(
        _state("SF2002 完成 11 个", role="production_employee", user_id=employee["id"], permissions=permissions),
        config={"configurable": {"thread_id": "d2-production-different-payload"}},
    )
    assert other_user["terminal_status"] == "PENDING_APPROVAL"
    assert different_payload["terminal_status"] == "PENDING_APPROVAL"

    # A different production task is a different business object even when
    # order and quantities happen to match.
    create_request(
        "production_report",
        employee["id"],
        {
            "order_no": "SF2002",
            "production_no": "MO-SF2002-ALT",
            "reported_quantity": 10,
            "qualified_quantity": 10,
            "rejected_quantity": 0,
            "report_date": "2026-10-09",
        },
    )
    with connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_requests WHERE request_type = 'production_report'"
        ).fetchone()[0] == 5
