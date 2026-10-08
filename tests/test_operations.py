from decimal import Decimal

import pytest

from app.auth import create_user, ensure_bootstrap_admin
from app.database import connection, initialize_database
from app.operations import create_request, decide_request, employee_dashboard, withdraw_request


def _login_users(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "operations.sqlite"))
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_USERNAME", "ops-admin")
    monkeypatch.setenv("SUPPORTFLOW_ADMIN_PASSWORD", "OpsAdmin-2026!")
    initialize_database()
    ensure_bootstrap_admin()
    employee = create_user("operator01", "Operator-2026!", "生产员工", "生产部")
    with connection() as conn:
        admin = dict(conn.execute("SELECT * FROM users WHERE username = 'ops-admin'").fetchone())
    return employee["id"], admin["id"]


def test_order_submission_requires_admin_approval(monkeypatch, tmp_path):
    employee_id, admin_id = _login_users(monkeypatch, tmp_path)
    submitted = create_request(
        "order_submission",
        employee_id,
        {
            "customer_code": "CUST-001",
            "sku": "REEL-7IN-BLACK",
            "quantity": 100,
            "required_date": "2026-10-31",
            "remark": "客户月度补单",
        },
    )
    assert submitted["status"] == "SUBMITTED"
    with connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM sales_orders WHERE order_no LIKE 'SF-NEW-%'"
        ).fetchone()[0] == 0

    approved = decide_request(submitted["id"], admin_id, True, "订单信息已核对")
    assert approved["status"] == "APPROVED"
    with connection() as conn:
        created = conn.execute(
            """SELECT so.order_no, so.status, po.production_no,
                po.planned_quantity, po.status AS production_status
            FROM sales_orders so
            JOIN sales_order_lines sol ON sol.order_id = so.id
            JOIN production_orders po ON po.order_line_id = sol.id
            WHERE so.order_no LIKE 'SF-NEW-%'
            ORDER BY so.id DESC LIMIT 1"""
        ).fetchone()
    assert created["status"] == "APPROVED"
    assert created["production_no"] == f"MO-{created['order_no']}"
    assert created["planned_quantity"] == 100
    assert created["production_status"] == "NOT_STARTED"
    assert approved["payload"]["order_no"] == created["order_no"]


def test_production_report_cannot_exceed_order_plan(monkeypatch, tmp_path):
    employee_id, admin_id = _login_users(monkeypatch, tmp_path)
    submitted = create_request(
        "production_report",
        employee_id,
        {
            "order_no": "SF2002",
            "reported_quantity": 1000,
            "qualified_quantity": 1000,
            "rejected_quantity": 0,
            "report_date": "2026-10-07",
            "remark": "异常报工测试",
        },
    )
    failed = decide_request(submitted["id"], admin_id, True)
    assert failed["status"] == "FAILED"
    assert "累计报工数量不能超过订单需求数量" in failed["rejection_reason"]


def test_outbound_mutates_inventory_only_after_approval(monkeypatch, tmp_path):
    employee_id, admin_id = _login_users(monkeypatch, tmp_path)
    with connection() as conn:
        before = conn.execute(
            "SELECT SUM(on_hand_quantity) FROM inventory_balances WHERE product_id = (SELECT id FROM products WHERE sku = 'REEL-13IN-BLACK')"
        ).fetchone()[0]
    submitted = create_request(
        "outbound_request",
        employee_id,
        {"order_no": "SF2002", "quantity": 10, "remark": "成品出库申请"},
    )
    with connection() as conn:
        assert conn.execute(
            "SELECT SUM(on_hand_quantity) FROM inventory_balances WHERE product_id = (SELECT id FROM products WHERE sku = 'REEL-13IN-BLACK')"
        ).fetchone()[0] == before

    approved = decide_request(submitted["id"], admin_id, True, "库存和订单已核对")
    assert approved["status"] == "APPROVED"
    with connection() as conn:
        after = conn.execute(
            "SELECT SUM(on_hand_quantity) FROM inventory_balances WHERE product_id = (SELECT id FROM products WHERE sku = 'REEL-13IN-BLACK')"
        ).fetchone()[0]
    assert after == before - 10


def test_employee_dashboard_exposes_own_work(monkeypatch, tmp_path):
    employee_id, _ = _login_users(monkeypatch, tmp_path)
    create_request(
        "quality_report",
        employee_id,
        {
            "order_no": "SF2002",
            "inspected_quantity": 10,
            "qualified_quantity": 9,
            "rejected_quantity": 1,
            "report_date": "2026-10-07",
            "remark": "抽检",
        },
    )
    dashboard = employee_dashboard(employee_id)
    assert dashboard["request_counts"]["SUBMITTED"] == 1
    assert dashboard["requests"][0]["request_type"] == "quality_report"


def test_operation_request_idempotency_returns_existing_request(monkeypatch, tmp_path):
    employee_id, _ = _login_users(monkeypatch, tmp_path)
    payload = {
        "order_no": "SF2002",
        "reported_quantity": 10,
        "qualified_quantity": 10,
        "rejected_quantity": 0,
    }
    first = create_request("production_report", employee_id, payload, "production-click-001")
    second = create_request("production_report", employee_id, payload, "production-click-001")
    assert second["id"] == first["id"]
    with connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM operation_requests WHERE idempotency_key = 'production-click-001'"
        ).fetchone()[0] == 1


def test_employee_can_withdraw_only_submitted_request(monkeypatch, tmp_path):
    employee_id, admin_id = _login_users(monkeypatch, tmp_path)
    submitted = create_request(
        "quality_report",
        employee_id,
        {
            "order_no": "SF2002",
            "inspected_quantity": 10,
            "qualified_quantity": 9,
            "rejected_quantity": 1,
        },
    )
    withdrawn = withdraw_request(submitted["id"], employee_id)
    assert withdrawn["status"] == "WITHDRAWN"
    with pytest.raises(ValueError, match="只有待审批申请可以撤回"):
        withdraw_request(submitted["id"], employee_id)
    with pytest.raises(ValueError, match="该申请已经处理过"):
        decide_request(submitted["id"], admin_id, True)


def test_production_report_change_and_withdraw_require_admin_approval(monkeypatch, tmp_path):
    employee_id, admin_id = _login_users(monkeypatch, tmp_path)
    submitted = create_request(
        "production_report",
        employee_id,
        {
            "order_no": "SF2002",
            "reported_quantity": 10,
            "qualified_quantity": 10,
            "rejected_quantity": 0,
        },
    )
    approved = decide_request(submitted["id"], admin_id, True, "审核通过")
    assert approved["status"] == "APPROVED"
    with connection() as conn:
        report = conn.execute(
            "SELECT id FROM production_reports WHERE operator_name = 'operator01' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    change = create_request(
        "production_report_change",
        employee_id,
        {
            "report_id": report["id"],
            "reported_quantity": 20,
            "qualified_quantity": 18,
            "rejected_quantity": 2,
        },
    )
    assert decide_request(change["id"], admin_id, True, "修正报工")["status"] == "APPROVED"
    with connection() as conn:
        assert conn.execute(
            "SELECT reported_quantity FROM production_reports WHERE id = ?", (report["id"],)
        ).fetchone()[0] == 20
    withdraw = create_request("production_report_withdraw", employee_id, {"report_id": report["id"]})
    assert decide_request(withdraw["id"], admin_id, True, "撤回报工")["status"] == "APPROVED"
    with connection() as conn:
        assert conn.execute(
            "SELECT status FROM production_reports WHERE id = ?", (report["id"],)
        ).fetchone()[0] == "VOIDED"
