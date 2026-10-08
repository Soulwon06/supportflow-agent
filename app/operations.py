"""Deterministic manufacturing-operation workflows.

LLM/chat can help users describe an action, but this module is the source of
truth for validation, approval, inventory mutation, and audit records.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .database import connection, initialize_database


REQUEST_TYPES = {
    "order_submission",
    "production_report",
    "quality_report",
    "outbound_request",
    "production_report_change",
    "production_report_withdraw",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_no(request_type: str) -> str:
    prefix = {
        "order_submission": "ORD",
        "production_report": "PRD",
        "production_report_change": "PRD-CHG",
        "production_report_withdraw": "PRD-WDR",
        "quality_report": "QC",
        "outbound_request": "OUT",
    }[request_type]
    return f"{prefix}-REQ-{uuid.uuid4().hex[:10].upper()}"


def _serialize(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _row(row) -> dict[str, Any]:
    if row is None:
        return {}
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json"))
    return item


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是有效数字") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field} 必须是非负有限数字")
    if result != result.to_integral_value():
        raise ValueError(f"{field} 必须是整数")
    return result


def _positive_quantity(value: Any, field: str) -> int:
    result = int(_decimal(value, field))
    if result <= 0:
        raise ValueError(f"{field} 必须大于 0")
    return result


def create_request(request_type: str, requested_by: int, payload: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
    initialize_database()
    if request_type not in REQUEST_TYPES:
        raise ValueError("不支持的申请类型")
    request_no = _request_no(request_type)
    idempotency_key = (idempotency_key or "").strip() or None
    with connection() as conn:
        if idempotency_key:
            existing = conn.execute("SELECT * FROM operation_requests WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing is not None:
                return _row(existing)
        cursor = conn.execute(
            """INSERT INTO operation_requests(
                request_no, request_type, status, requested_by, payload_json, idempotency_key
            ) VALUES (?, ?, 'SUBMITTED', ?, ?, ?)""",
            (request_no, request_type, requested_by, _serialize(payload), idempotency_key),
        )
        conn.execute(
            """INSERT INTO audit_logs(actor, action, resource_type, resource_id, result)
            VALUES (?, 'OPERATION_REQUEST_SUBMIT', 'operation_request', ?, 'SUBMITTED')""",
            (str(requested_by), request_no),
        )
        row = conn.execute(
            "SELECT * FROM operation_requests WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _row(row)


def list_requests(*, requested_by: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
    initialize_database()
    clauses: list[str] = []
    params: list[Any] = []
    if requested_by is not None:
        clauses.append("r.requested_by = ?")
        params.append(requested_by)
    if status:
        clauses.append("r.status = ?")
        params.append(status)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with connection() as conn:
        rows = conn.execute(
            f"""SELECT r.*, u.username AS requester_username,
                u.display_name AS requester_name
            FROM operation_requests r JOIN users u ON u.id = r.requested_by
            {where} ORDER BY r.id DESC""",
            params,
        ).fetchall()
        return [_row(row) for row in rows]


def withdraw_request(request_id: int, requested_by: int) -> dict[str, Any]:
    initialize_database()
    with connection() as conn:
        row = conn.execute("SELECT * FROM operation_requests WHERE id = ? AND requested_by = ?", (request_id, requested_by)).fetchone()
        if row is None:
            raise LookupError("申请不存在")
        if row["status"] != "SUBMITTED":
            raise ValueError("只有待审批申请可以撤回")
        conn.execute("UPDATE operation_requests SET status = 'WITHDRAWN', withdrawn_at = CURRENT_TIMESTAMP, withdrawn_by = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (requested_by, request_id))
        conn.execute("INSERT INTO audit_logs(actor, action, resource_type, resource_id, result) VALUES (?, 'OPERATION_REQUEST_WITHDRAW', 'operation_request', ?, 'WITHDRAWN')", (str(requested_by), row["request_no"]))
        return _row(conn.execute("SELECT * FROM operation_requests WHERE id = ?", (request_id,)).fetchone())


def employee_dashboard(user_id: int) -> dict[str, Any]:
    initialize_database()
    requests = list_requests(requested_by=user_id)
    with connection() as conn:
        username = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()[0]
        production = conn.execute(
            """SELECT COALESCE(SUM(pr.reported_quantity), 0) AS reported,
                COALESCE(SUM(pr.qualified_quantity), 0) AS qualified,
                COALESCE(SUM(pr.rejected_quantity), 0) AS rejected
            FROM production_reports pr WHERE pr.status = 'ACTIVE' AND pr.operator_name = (
                SELECT username FROM users WHERE id = ?
            )""",
            (user_id,),
        ).fetchone()
        quality = conn.execute(
            """SELECT COALESCE(SUM(inspected_quantity), 0) AS inspected,
                COALESCE(SUM(qualified_quantity), 0) AS qualified,
                COALESCE(SUM(rejected_quantity), 0) AS rejected
            FROM quality_reports WHERE inspector_id = ?""",
            (user_id,),
        ).fetchone()
        production_reports = conn.execute(
            """SELECT pr.id, po.production_no, so.order_no, p.sku,
                pr.report_date, pr.reported_quantity, pr.qualified_quantity,
                pr.rejected_quantity, pr.status, pr.remark
            FROM production_reports pr
            JOIN production_orders po ON po.id = pr.production_order_id
            JOIN sales_order_lines sol ON sol.id = po.order_line_id
            JOIN sales_orders so ON so.id = sol.order_id
            JOIN products p ON p.id = sol.product_id
            WHERE pr.operator_name = ? AND pr.status = 'ACTIVE'
            ORDER BY pr.id DESC LIMIT 20""",
            (username,),
        ).fetchall()
    counts = {status: sum(1 for item in requests if item["status"] == status) for status in ("SUBMITTED", "APPROVED", "REJECTED", "FAILED")}
    return {
        "request_counts": counts,
        "requests": requests[:20],
        "production": dict(production),
        "quality": dict(quality),
        "production_reports": [dict(row) for row in production_reports],
    }


def admin_dashboard(
    *,
    employee_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict[str, Any]:
    initialize_database()
    with connection() as conn:
        request_clauses: list[str] = []
        request_params: list[Any] = []
        if employee_id is not None:
            request_clauses.append("r.requested_by = ?")
            request_params.append(employee_id)
        if status:
            request_clauses.append("r.status = ?")
            request_params.append(status)
        if date_from:
            request_clauses.append("date(r.created_at) >= date(?)")
            request_params.append(date_from)
        if date_to:
            request_clauses.append("date(r.created_at) <= date(?)")
            request_params.append(date_to)
        request_where = "WHERE " + " AND ".join(request_clauses) if request_clauses else ""

        order_clauses: list[str] = []
        order_params: list[Any] = []
        if status:
            order_clauses.append("so.status = ?")
            order_params.append(status)
        if date_from:
            order_clauses.append("date(so.required_date) >= date(?)")
            order_params.append(date_from)
        if date_to:
            order_clauses.append("date(so.required_date) <= date(?)")
            order_params.append(date_to)
        order_where = "WHERE " + " AND ".join(order_clauses) if order_clauses else "WHERE 1 = 1"

        order_status = [dict(row) for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM sales_orders GROUP BY status ORDER BY status"
        )]
        request_status = [dict(row) for row in conn.execute(
            "SELECT request_type, status, COUNT(*) AS count FROM operation_requests GROUP BY request_type, status ORDER BY request_type, status"
        )]
        production = dict(conn.execute(
            """SELECT COALESCE(SUM(sol.ordered_quantity), 0) AS ordered,
                COALESCE(SUM(sol.completed_quantity), 0) AS completed,
                COALESCE(SUM(sol.shipped_quantity), 0) AS shipped
            FROM sales_order_lines sol JOIN sales_orders so ON so.id = sol.order_id
            WHERE so.status NOT IN ('CANCELLED')"""
        ).fetchone())
        quality = dict(conn.execute(
            """SELECT COALESCE(SUM(inspected_quantity), 0) AS inspected,
                COALESCE(SUM(qualified_quantity), 0) AS qualified,
                COALESCE(SUM(rejected_quantity), 0) AS rejected
            FROM quality_reports"""
        ).fetchone())
        inventory = dict(conn.execute(
            """SELECT COALESCE(SUM(on_hand_quantity), 0) AS on_hand,
                COALESCE(SUM(reserved_quantity), 0) AS reserved,
                COALESCE(SUM(on_hand_quantity - reserved_quantity), 0) AS available
            FROM inventory_balances"""
        ).fetchone())
        orders = conn.execute(
            """SELECT so.order_no, c.name AS customer_name, so.status,
                so.required_date, p.sku, p.product_name,
                sol.ordered_quantity, sol.completed_quantity, sol.shipped_quantity
            FROM sales_orders so
            JOIN customers c ON c.id = so.customer_id
            JOIN sales_order_lines sol ON sol.order_id = so.id
            JOIN products p ON p.id = sol.product_id
            {order_where}
            ORDER BY so.required_date, so.id DESC""".format(order_where=order_where),
            order_params,
        ).fetchall()
        recent = conn.execute(
            """SELECT r.request_no, r.request_type, r.status, r.created_at,
                r.id, r.requested_by, u.display_name AS requester_name,
                u.username AS requester_username
            FROM operation_requests r JOIN users u ON u.id = r.requested_by
            {request_where}
            ORDER BY r.id DESC LIMIT 50""".format(request_where=request_where),
            request_params,
        ).fetchall()
        employee_workload = conn.execute(
            """SELECT u.id, u.username, u.display_name,
                COALESCE(SUM(CASE WHEN r.request_type = 'production_report' AND r.status = 'APPROVED' THEN 1 ELSE 0 END), 0) AS production_requests,
                COALESCE(SUM(CASE WHEN r.request_type = 'quality_report' AND r.status = 'APPROVED' THEN 1 ELSE 0 END), 0) AS quality_requests,
                COALESCE(SUM(CASE WHEN r.status = 'SUBMITTED' THEN 1 ELSE 0 END), 0) AS pending_requests
            FROM users u LEFT JOIN operation_requests r ON r.requested_by = u.id
            GROUP BY u.id ORDER BY u.id"""
        ).fetchall()
        approval_history = conn.execute(
            """SELECT a.id, a.request_id, r.request_no, r.request_type,
                a.decision, a.reason, a.created_at,
                u.display_name AS approver_name
            FROM approval_records a
            JOIN operation_requests r ON r.id = a.request_id
            JOIN users u ON u.id = a.approver_id
            ORDER BY a.id DESC LIMIT 50"""
        ).fetchall()
        today_production = dict(conn.execute(
            """SELECT COALESCE(SUM(reported_quantity), 0) AS reported,
                COALESCE(SUM(qualified_quantity), 0) AS qualified,
                COALESCE(SUM(rejected_quantity), 0) AS rejected
            FROM production_reports WHERE status = 'ACTIVE' AND report_date = date('now')"""
        ).fetchone())
        today_quality = dict(conn.execute(
            """SELECT COALESCE(SUM(inspected_quantity), 0) AS inspected,
                COALESCE(SUM(qualified_quantity), 0) AS qualified,
                COALESCE(SUM(rejected_quantity), 0) AS rejected
            FROM quality_reports WHERE report_date = date('now')"""
        ).fetchone())
    return {
        "order_status": order_status,
        "request_status": request_status,
        "production": production,
        "quality": quality,
        "inventory": inventory,
        "orders": [dict(row) for row in orders],
        "recent_requests": [dict(row) for row in recent],
        "employee_workload": [dict(row) for row in employee_workload],
        "approval_history": [dict(row) for row in approval_history],
        "today": {"production": today_production, "quality": today_quality},
    }


def _record_approval(conn, request_id: int, approver_id: int, decision: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO approval_records(request_id, approver_id, decision, reason)
        VALUES (?, ?, ?, ?)""",
        (request_id, approver_id, decision, reason),
    )


def _approve_order(conn, payload: dict[str, Any], requester_id: int) -> str:
    customer_code = str(payload.get("customer_code", "")).strip().upper()
    sku = str(payload.get("sku", "")).strip().upper()
    quantity = _positive_quantity(payload.get("quantity"), "订单数量")
    required_date = str(payload.get("required_date", "")).strip()
    if not required_date:
        raise ValueError("交期不能为空")
    customer = conn.execute(
        "SELECT id, name FROM customers WHERE customer_code = ? AND status = 'ACTIVE'",
        (customer_code,),
    ).fetchone()
    product = conn.execute(
        "SELECT id FROM products WHERE sku = ? AND status = 'ACTIVE'", (sku,)
    ).fetchone()
    if customer is None:
        raise ValueError("客户不存在")
    if product is None:
        raise ValueError("产品不存在")
    price = {
        "REEL-7IN-BLACK": Decimal("18.00"),
        "REEL-7IN-CLEAR": Decimal("21.00"),
        "REEL-13IN-BLACK": Decimal("55.00"),
        "REEL-13IN-CLEAR": Decimal("33.00"),
        "REEL-15IN-ESD": Decimal("52.50"),
        "REEL-22IN-ESD": Decimal("103.00"),
        "REEL-5IN-BLACK": Decimal("12.00"),
        "REEL-7IN-WHITE": Decimal("22.00"),
    }.get(sku)
    if price is None:
        raise ValueError("产品没有配置单价")
    order_no = f"SF-NEW-{conn.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM sales_orders').fetchone()[0]:04d}"
    total = price * quantity
    cursor = conn.execute(
        """INSERT INTO sales_orders(
            order_no, customer_id, status, order_date, required_date,
            total_amount, created_by
        ) VALUES (?, ?, 'APPROVED', DATE('now'), ?, ?, ?)""",
        (order_no, customer["id"], required_date, str(total), str(requester_id)),
    )
    spec = conn.execute(
        "SELECT * FROM product_specs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
        (product["id"],),
    ).fetchone()
    line_cursor = conn.execute(
        """INSERT INTO sales_order_lines(
            order_id, product_id, product_spec_snapshot, ordered_quantity, unit_price
        ) VALUES (?, ?, ?, ?, ?)""",
        (cursor.lastrowid, product["id"], str(dict(spec)), quantity, str(price)),
    )
    # Every approved sales order must have a production task before employees
    # can submit production or quality reports.  Keep this creation in the
    # shared approval path so employee-submitted orders and administrator-
    # created orders follow the same lifecycle.
    conn.execute(
        """INSERT INTO production_orders(
            production_no, order_line_id, planned_quantity, status,
            planned_start_at, planned_end_at
        ) VALUES (?, ?, ?, 'NOT_STARTED', ?, ?)""",
        (
            f"MO-{order_no}",
            line_cursor.lastrowid,
            quantity,
            str(date.today()),
            required_date,
        ),
    )
    return order_no


def _approve_production(conn, payload: dict[str, Any], requester_id: int) -> None:
    order_no = str(payload.get("order_no", "")).strip().upper()
    reported = _positive_quantity(payload.get("reported_quantity"), "完成数量")
    qualified = _positive_quantity(payload.get("qualified_quantity", 0), "合格数量") if payload.get("qualified_quantity", 0) else 0
    rejected = _positive_quantity(payload.get("rejected_quantity", 0), "不良数量") if payload.get("rejected_quantity", 0) else 0
    if qualified + rejected > reported:
        raise ValueError("合格数量与不良数量不能超过完成数量")
    row = conn.execute(
        """SELECT po.id, po.planned_quantity, po.status,
            COALESCE(SUM(CASE WHEN pr.status = 'ACTIVE' THEN pr.reported_quantity ELSE 0 END), 0) AS reported_total
        FROM production_orders po JOIN sales_order_lines sol ON sol.id = po.order_line_id
        JOIN sales_orders so ON so.id = sol.order_id
        LEFT JOIN production_reports pr ON pr.production_order_id = po.id
        WHERE so.order_no = ? GROUP BY po.id""",
        (order_no,),
    ).fetchone()
    if row is None:
        raise ValueError("订单没有对应生产任务")
    if int(row["reported_total"]) + reported > int(row["planned_quantity"]):
        raise ValueError("累计报工数量不能超过订单需求数量")
    conn.execute(
        """INSERT INTO production_reports(
            production_order_id, report_date, reported_quantity,
            qualified_quantity, rejected_quantity, operator_name, remark
        ) VALUES (?, ?, ?, ?, ?, (SELECT username FROM users WHERE id = ?), ?)""",
        (row["id"], str(payload.get("report_date") or date.today()), reported, qualified, rejected, requester_id, str(payload.get("remark") or "")),
    )
    new_total = int(row["reported_total"]) + reported
    new_status = "COMPLETED" if new_total == int(row["planned_quantity"]) else "IN_PROGRESS"
    conn.execute("UPDATE production_orders SET status = ? WHERE id = ?", (new_status, row["id"]))
    conn.execute("UPDATE sales_order_lines SET completed_quantity = ? WHERE id = (SELECT order_line_id FROM production_orders WHERE id = ?)", (new_total, row["id"]))


def _approve_quality(conn, payload: dict[str, Any], requester_id: int) -> None:
    order_no = str(payload.get("order_no", "")).strip().upper()
    inspected = _positive_quantity(payload.get("inspected_quantity"), "检验数量")
    qualified = _positive_quantity(payload.get("qualified_quantity", 0), "合格数量") if payload.get("qualified_quantity", 0) else 0
    rejected = _positive_quantity(payload.get("rejected_quantity", 0), "不合格数量") if payload.get("rejected_quantity", 0) else 0
    if qualified + rejected != inspected:
        raise ValueError("合格数量加不合格数量必须等于检验数量")
    order = conn.execute("SELECT id FROM sales_orders WHERE order_no = ?", (order_no,)).fetchone()
    if order is None:
        raise ValueError("订单不存在")
    produced = conn.execute(
        """SELECT COALESCE(SUM(pr.reported_quantity), 0) AS quantity
        FROM production_reports pr JOIN production_orders po ON po.id = pr.production_order_id
        JOIN sales_order_lines sol ON sol.id = po.order_line_id
        WHERE sol.order_id = ? AND pr.status = 'ACTIVE'""",
        (order["id"],),
    ).fetchone()["quantity"]
    if inspected > int(produced):
        raise ValueError("检验数量不能超过已报工数量")
    conn.execute(
        """INSERT INTO quality_reports(
            order_id, inspected_quantity, qualified_quantity, rejected_quantity,
            inspector_id, report_date, remark
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (order["id"], inspected, qualified, rejected, requester_id, str(payload.get("report_date") or date.today()), str(payload.get("remark") or "")),
    )


def _approve_outbound(conn, payload: dict[str, Any], requester_id: int) -> None:
    order_no = str(payload.get("order_no", "")).strip().upper()
    quantity = _positive_quantity(payload.get("quantity"), "出库数量")
    row = conn.execute(
        """SELECT so.id AS order_id, sol.id AS line_id, sol.product_id,
            sol.ordered_quantity, sol.shipped_quantity, sol.cancelled_quantity,
            p.sku
        FROM sales_orders so JOIN sales_order_lines sol ON sol.order_id = so.id
        JOIN products p ON p.id = sol.product_id WHERE so.order_no = ?""",
        (order_no,),
    ).fetchone()
    if row is None:
        raise ValueError("订单不存在")
    remaining = int(row["ordered_quantity"]) - int(row["shipped_quantity"]) - int(row["cancelled_quantity"])
    if quantity > remaining:
        raise ValueError("出库数量不能超过订单剩余数量")
    lots = conn.execute(
        """SELECT id, warehouse_id, lot_no, on_hand_quantity, reserved_quantity
        FROM inventory_balances WHERE product_id = ? AND on_hand_quantity - reserved_quantity > 0
        ORDER BY id""",
        (row["product_id"],),
    ).fetchall()
    available = sum(int(lot["on_hand_quantity"]) - int(lot["reserved_quantity"]) for lot in lots)
    if quantity > available:
        raise ValueError("当前可用库存不足，不能出库")
    remaining_to_ship = quantity
    for lot in lots:
        take = min(remaining_to_ship, int(lot["on_hand_quantity"]) - int(lot["reserved_quantity"]))
        if take <= 0:
            continue
        conn.execute("UPDATE inventory_balances SET on_hand_quantity = on_hand_quantity - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (take, lot["id"]))
        conn.execute(
            """INSERT INTO inventory_transactions(
                product_id, warehouse_id, lot_no, transaction_type, quantity,
                reference_type, reference_id, created_by
            ) VALUES (?, ?, ?, 'OUTBOUND', ?, 'sales_order', ?, ?)""",
            (row["product_id"], lot["warehouse_id"], lot["lot_no"], -take, order_no, str(requester_id)),
        )
        remaining_to_ship -= take
        if remaining_to_ship == 0:
            break
    conn.execute("UPDATE sales_order_lines SET shipped_quantity = shipped_quantity + ? WHERE id = ?", (quantity, row["line_id"]))
    new_shipped = int(row["shipped_quantity"]) + quantity
    status = "SHIPPED" if new_shipped >= remaining + int(row["shipped_quantity"]) else "PARTIALLY_COMPLETED"
    conn.execute("UPDATE sales_orders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, row["order_id"]))
    conn.execute(
        "INSERT INTO logistics_events(order_id, status, location, event_time) VALUES (?, ?, '深圳成品仓', CURRENT_TIMESTAMP)",
        (row["order_id"], "SHIPPED" if status == "SHIPPED" else "PARTIAL_SHIPMENT"),
    )


def _refresh_production_totals(conn, production_order_id: int) -> None:
    row = conn.execute("SELECT planned_quantity, order_line_id FROM production_orders WHERE id = ?", (production_order_id,)).fetchone()
    if row is None:
        raise ValueError("生产任务不存在")
    total = conn.execute("SELECT COALESCE(SUM(reported_quantity), 0) FROM production_reports WHERE production_order_id = ? AND status = 'ACTIVE'", (production_order_id,)).fetchone()[0]
    status = "COMPLETED" if int(total) == int(row["planned_quantity"]) else "IN_PROGRESS"
    conn.execute("UPDATE production_orders SET status = ? WHERE id = ?", (status, production_order_id))
    conn.execute("UPDATE sales_order_lines SET completed_quantity = ? WHERE id = ?", (int(total), row["order_line_id"]))


def _approve_production_change(conn, payload: dict[str, Any], requester_id: int) -> None:
    report_id = int(payload.get("report_id") or 0)
    report = conn.execute("SELECT * FROM production_reports WHERE id = ? AND status = 'ACTIVE'", (report_id,)).fetchone()
    if report is None:
        raise ValueError("生产报工不存在或已经撤回")
    requester = conn.execute("SELECT username FROM users WHERE id = ?", (requester_id,)).fetchone()
    if requester is None or report["operator_name"] != requester["username"]:
        raise ValueError("只能申请修改自己的生产报工")
    reported = _positive_quantity(payload.get("reported_quantity"), "完成数量")
    qualified = _positive_quantity(payload.get("qualified_quantity", 0), "合格数量") if payload.get("qualified_quantity", 0) else 0
    rejected = _positive_quantity(payload.get("rejected_quantity", 0), "不良数量") if payload.get("rejected_quantity", 0) else 0
    if qualified + rejected > reported:
        raise ValueError("合格数量与不良数量不能超过完成数量")
    planned = conn.execute("SELECT planned_quantity FROM production_orders WHERE id = ?", (report["production_order_id"],)).fetchone()[0]
    other_total = conn.execute("SELECT COALESCE(SUM(reported_quantity), 0) FROM production_reports WHERE production_order_id = ? AND status = 'ACTIVE' AND id <> ?", (report["production_order_id"], report_id)).fetchone()[0]
    if int(other_total) + reported > int(planned):
        raise ValueError("修改后累计报工数量不能超过订单需求数量")
    conn.execute("UPDATE production_reports SET reported_quantity = ?, qualified_quantity = ?, rejected_quantity = ?, remark = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (reported, qualified, rejected, str(payload.get("remark") or "修改报工"), report_id))
    _refresh_production_totals(conn, report["production_order_id"])


def _approve_production_withdraw(conn, payload: dict[str, Any], requester_id: int) -> None:
    report_id = int(payload.get("report_id") or 0)
    report = conn.execute("SELECT * FROM production_reports WHERE id = ? AND status = 'ACTIVE'", (report_id,)).fetchone()
    if report is None:
        raise ValueError("生产报工不存在或已经撤回")
    requester = conn.execute("SELECT username FROM users WHERE id = ?", (requester_id,)).fetchone()
    if requester is None or report["operator_name"] != requester["username"]:
        raise ValueError("只能申请撤回自己的生产报工")
    conn.execute("UPDATE production_reports SET status = 'VOIDED', voided_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (report_id,))
    _refresh_production_totals(conn, report["production_order_id"])


def decide_request(request_id: int, approver_id: int, approved: bool, reason: str = "") -> dict[str, Any]:
    initialize_database()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        request = conn.execute("SELECT * FROM operation_requests WHERE id = ?", (request_id,)).fetchone()
        if request is None:
            raise LookupError("申请不存在")
        if request["status"] != "SUBMITTED":
            raise ValueError("该申请已经处理过")
        payload = json.loads(request["payload_json"])
        decision = "APPROVED" if approved else "REJECTED"
        if not approved:
            if not reason.strip():
                raise ValueError("驳回时必须填写原因")
            conn.execute("UPDATE operation_requests SET status = 'REJECTED', approved_by = ?, rejection_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (approver_id, reason.strip(), request_id))
            _record_approval(conn, request_id, approver_id, decision, reason.strip())
        else:
            try:
                if request["request_type"] == "order_submission":
                    payload["order_no"] = _approve_order(conn, payload, request["requested_by"])
                elif request["request_type"] == "production_report":
                    _approve_production(conn, payload, request["requested_by"])
                elif request["request_type"] == "quality_report":
                    _approve_quality(conn, payload, request["requested_by"])
                elif request["request_type"] == "outbound_request":
                    _approve_outbound(conn, payload, request["requested_by"])
                elif request["request_type"] == "production_report_change":
                    _approve_production_change(conn, payload, request["requested_by"])
                elif request["request_type"] == "production_report_withdraw":
                    _approve_production_withdraw(conn, payload, request["requested_by"])
                conn.execute("UPDATE operation_requests SET status = 'APPROVED', approved_by = ?, payload_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (approver_id, _serialize(payload), request_id))
                _record_approval(conn, request_id, approver_id, decision, reason.strip())
            except (ValueError, LookupError) as exc:
                message = str(exc)
                conn.execute("UPDATE operation_requests SET status = 'FAILED', approved_by = ?, rejection_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (approver_id, message, request_id))
                _record_approval(conn, request_id, approver_id, "FAILED", message)
        updated = conn.execute("SELECT * FROM operation_requests WHERE id = ?", (request_id,)).fetchone()
        return _row(updated)

