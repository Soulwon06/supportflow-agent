"""Synthetic business database for the SupportFlow manufacturing demo.

The project intentionally uses generated data only.  This module keeps the
demo business tools backed by a small SQLite database so that order, inventory,
production, and refund answers have a durable source of truth without requiring
an external database service for local development.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Iterator


DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "data" / "supportflow_demo.db"


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    material TEXT NOT NULL,
    product_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS product_specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    version TEXT NOT NULL,
    reel_diameter TEXT NOT NULL,
    reel_width TEXT NOT NULL,
    core_diameter TEXT NOT NULL,
    flange_diameter TEXT NOT NULL,
    color TEXT NOT NULL,
    antistatic TEXT NOT NULL,
    cleanliness_requirement TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    UNIQUE(product_id, version)
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    location TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS sales_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL,
    order_date TEXT NOT NULL,
    required_date TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    total_amount NUMERIC NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sales_order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_spec_snapshot TEXT NOT NULL,
    ordered_quantity INTEGER NOT NULL CHECK (ordered_quantity > 0),
    unit_price NUMERIC NOT NULL,
    completed_quantity INTEGER NOT NULL DEFAULT 0,
    shipped_quantity INTEGER NOT NULL DEFAULT 0,
    cancelled_quantity INTEGER NOT NULL DEFAULT 0,
    line_status TEXT NOT NULL DEFAULT 'OPEN'
);

CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_no TEXT NOT NULL UNIQUE,
    order_line_id INTEGER NOT NULL REFERENCES sales_order_lines(id),
    planned_quantity INTEGER NOT NULL,
    status TEXT NOT NULL,
    planned_start_at TEXT NOT NULL,
    planned_end_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    production_order_id INTEGER NOT NULL REFERENCES production_orders(id),
    report_date TEXT NOT NULL,
    reported_quantity INTEGER NOT NULL,
    qualified_quantity INTEGER NOT NULL,
    rejected_quantity INTEGER NOT NULL,
    operator_name TEXT NOT NULL,
    remark TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    voided_at TEXT
);

CREATE TABLE IF NOT EXISTS logistics_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    status TEXT NOT NULL,
    location TEXT NOT NULL,
    event_time TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_balances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    lot_no TEXT NOT NULL,
    on_hand_quantity INTEGER NOT NULL DEFAULT 0,
    reserved_quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(product_id, warehouse_id, lot_no)
);

CREATE TABLE IF NOT EXISTS inventory_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    lot_no TEXT NOT NULL,
    transaction_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    reference_type TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refund_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refund_no TEXT NOT NULL UNIQUE,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    reason TEXT NOT NULL,
    requested_amount NUMERIC NOT NULL,
    approved_amount NUMERIC,
    status TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS refund_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    refund_request_id INTEGER NOT NULL REFERENCES refund_requests(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    executed_amount NUMERIC NOT NULL,
    provider_reference TEXT NOT NULL,
    status TEXT NOT NULL,
    executed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operation_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT NOT NULL UNIQUE,
    request_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'SUBMITTED',
    requested_by INTEGER NOT NULL REFERENCES users(id),
    payload_json TEXT NOT NULL,
    approved_by INTEGER REFERENCES users(id),
    rejection_reason TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    withdrawn_at TEXT,
    withdrawn_by INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS quality_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    inspected_quantity INTEGER NOT NULL CHECK (inspected_quantity > 0),
    qualified_quantity INTEGER NOT NULL CHECK (qualified_quantity >= 0),
    rejected_quantity INTEGER NOT NULL CHECK (rejected_quantity >= 0),
    inspector_id INTEGER NOT NULL REFERENCES users(id),
    report_date TEXT NOT NULL,
    remark TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (qualified_quantity + rejected_quantity = inspected_quantity)
);

CREATE TABLE IF NOT EXISTS approval_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES operation_requests(id),
    approver_id INTEGER NOT NULL REFERENCES users(id),
    decision TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',
    department TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    last_login_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_permissions (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    permission TEXT NOT NULL,
    granted_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, permission)
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sales_orders_customer ON sales_orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_order_lines_order ON sales_order_lines(order_id);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory_balances(product_id);
CREATE INDEX IF NOT EXISTS idx_production_order_line ON production_orders(order_line_id);
CREATE INDEX IF NOT EXISTS idx_refund_order ON refund_requests(order_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_operation_requests_status ON operation_requests(status);
CREATE INDEX IF NOT EXISTS idx_operation_requests_user ON operation_requests(requested_by);
CREATE INDEX IF NOT EXISTS idx_quality_reports_order ON quality_reports(order_id);
"""


CUSTOMERS = [
    ("CUST-001", "华南封装科技（虚构）", "陈工", "13800000001", "深圳市宝安区示范工业园"),
    ("CUST-002", "东莞芯联电子（虚构）", "刘工", "13800000002", "东莞市松山湖示范园"),
    ("CUST-003", "苏州微封装（虚构）", "周工", "13800000003", "苏州市工业园区示范路"),
    ("CUST-004", "厦门精密封测（虚构）", "林工", "13800000004", "厦门市海沧区示范园"),
]


PRODUCTS = [
    ("REEL-7IN-BLACK", "7英寸黑色塑胶载体卷轴", "PS", "半导体包装卷轴"),
    ("REEL-7IN-CLEAR", "7英寸透明塑胶载体卷轴", "PS", "半导体包装卷轴"),
    ("REEL-13IN-BLACK", "13英寸黑色塑胶载体卷轴", "PS", "半导体包装卷轴"),
    ("REEL-13IN-CLEAR", "13英寸透明塑胶载体卷轴", "PC", "半导体包装卷轴"),
    ("REEL-15IN-ESD", "15英寸防静电塑胶载体卷轴", "ESD-PS", "半导体包装卷轴"),
    ("REEL-22IN-ESD", "22英寸防静电塑胶载体卷轴", "ESD-PS", "半导体包装卷轴"),
    ("REEL-5IN-BLACK", "5英寸黑色塑胶载体卷轴", "PS", "半导体包装卷轴"),
    ("REEL-7IN-WHITE", "7英寸白色塑胶载体卷轴", "PS", "半导体包装卷轴"),
]


PRODUCT_SPECS = [
    ("REEL-7IN-BLACK", "V2", "7 inch", "8 mm", "76 mm", "178 mm", "黑色", "否", "常规洁净包装"),
    ("REEL-7IN-CLEAR", "V1", "7 inch", "8 mm", "76 mm", "178 mm", "透明", "否", "洁净室二次包装"),
    ("REEL-13IN-BLACK", "V3", "13 inch", "12 mm", "76 mm", "330 mm", "黑色", "否", "常规洁净包装"),
    ("REEL-13IN-CLEAR", "V1", "13 inch", "16 mm", "76 mm", "330 mm", "透明", "否", "洁净室二次包装"),
    ("REEL-15IN-ESD", "V2", "15 inch", "16 mm", "76 mm", "380 mm", "深灰", "是", "ESD洁净包装"),
    ("REEL-22IN-ESD", "V1", "22 inch", "24 mm", "76 mm", "560 mm", "深灰", "是", "ESD洁净包装"),
    ("REEL-5IN-BLACK", "V1", "5 inch", "8 mm", "76 mm", "130 mm", "黑色", "否", "常规洁净包装"),
    ("REEL-7IN-WHITE", "V1", "7 inch", "8 mm", "76 mm", "178 mm", "白色", "否", "常规洁净包装"),
]


ORDERS = [
    ("SF1001", "CUST-001", "SHIPPED", "2026-08-01", "2026-08-15", "18000.00", "sales_demo"),
    ("SF2002", "CUST-002", "IN_PRODUCTION", "2026-08-05", "2026-08-22", "27500.00", "sales_demo"),
    ("SF3003", "CUST-003", "COMPLETED", "2026-08-08", "2026-08-20", "42000.00", "sales_demo"),
    ("SF4004", "CUST-004", "CONFIRMED", "2026-08-12", "2026-08-30", "12600.00", "sales_demo"),
    ("SF5005", "CUST-001", "PARTIALLY_COMPLETED", "2026-08-15", "2026-09-03", "33000.00", "sales_demo"),
    ("SF6006", "CUST-002", "DRAFT", "2026-08-18", "2026-09-10", "9600.00", "sales_demo"),
    ("SF7007", "CUST-003", "CANCELLED", "2026-08-20", "2026-09-12", "8800.00", "sales_demo"),
    ("SF8008", "CUST-004", "IN_PRODUCTION", "2026-08-21", "2026-09-15", "51500.00", "sales_demo"),
    ("SF9009", "CUST-001", "SHIPPED", "2026-08-23", "2026-09-01", "15400.00", "sales_demo"),
    # Compatibility orders retained for the original SupportFlow regression tests.
    ("9527", "CUST-001", "SHIPPED", "2026-07-01", "2026-07-10", "500.00", "legacy_demo"),
    ("1001", "CUST-002", "CONFIRMED", "2026-07-02", "2026-07-12", "199.00", "legacy_demo"),
]


ORDER_LINES = [
    ("SF1001", "REEL-7IN-BLACK", 1000, "18.00", 1000, 1000, 0),
    ("SF2002", "REEL-13IN-BLACK", 500, "55.00", 320, 0, 0),
    ("SF3003", "REEL-15IN-ESD", 800, "52.50", 800, 800, 0),
    ("SF4004", "REEL-7IN-CLEAR", 600, "21.00", 0, 0, 0),
    ("SF5005", "REEL-13IN-CLEAR", 1000, "33.00", 420, 200, 0),
    ("SF6006", "REEL-5IN-BLACK", 800, "12.00", 0, 0, 0),
    ("SF7007", "REEL-7IN-WHITE", 400, "22.00", 0, 0, 400),
    ("SF8008", "REEL-22IN-ESD", 500, "103.00", 180, 0, 0),
    ("SF9009", "REEL-7IN-BLACK", 700, "22.00", 700, 700, 0),
    ("9527", "REEL-7IN-BLACK", 25, "20.00", 25, 25, 0),
    ("1001", "REEL-7IN-CLEAR", 10, "19.90", 0, 0, 0),
]


def database_path() -> Path:
    raw = os.getenv("SUPPORTFLOW_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))
    return Path(raw).expanduser()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    path = database_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize_database() -> None:
    with connection() as conn:
        conn.executescript(SCHEMA)
        _ensure_operations_schema(conn)
        already_seeded = conn.execute(
            "SELECT value FROM schema_metadata WHERE key = 'seed_version'"
        ).fetchone()
        if already_seeded:
            _ensure_historical_demo_refund(conn)
            _ensure_quality_demo_data(conn)
            return
        _seed(conn)
        conn.execute(
            "INSERT INTO schema_metadata(key, value) VALUES ('seed_version', 'synthetic-v1')"
        )


def _ensure_operations_schema(conn: sqlite3.Connection) -> None:
    """Apply additive operation/report migrations without changing deployment DB."""
    request_columns = {row[1] for row in conn.execute("PRAGMA table_info(operation_requests)")}
    for name, statement in {
        "idempotency_key": "ALTER TABLE operation_requests ADD COLUMN idempotency_key TEXT",
        "withdrawn_at": "ALTER TABLE operation_requests ADD COLUMN withdrawn_at TEXT",
        "withdrawn_by": "ALTER TABLE operation_requests ADD COLUMN withdrawn_by INTEGER",
    }.items():
        if name not in request_columns:
            conn.execute(statement)
    report_columns = {row[1] for row in conn.execute("PRAGMA table_info(production_reports)")}
    if "status" not in report_columns:
        conn.execute("ALTER TABLE production_reports ADD COLUMN status TEXT NOT NULL DEFAULT 'ACTIVE'")
    if "updated_at" not in report_columns:
        conn.execute("ALTER TABLE production_reports ADD COLUMN updated_at TEXT")
        conn.execute("UPDATE production_reports SET updated_at = CURRENT_TIMESTAMP WHERE updated_at IS NULL")
    if "voided_at" not in report_columns:
        conn.execute("ALTER TABLE production_reports ADD COLUMN voided_at TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_operation_requests_idempotency ON operation_requests(idempotency_key) WHERE idempotency_key IS NOT NULL")


def _ensure_historical_demo_refund(conn: sqlite3.Connection) -> None:
    """Add the synthetic historical refund when upgrading an older demo DB."""

    exists = conn.execute(
        "SELECT 1 FROM refund_executions WHERE idempotency_key = 'seed-refund-sf9009'"
    ).fetchone()
    if exists:
        return
    refund_request = conn.execute(
        """INSERT INTO refund_requests(
            refund_no, order_id, reason, requested_amount,
            approved_amount, status, requested_by
        ) VALUES ('RF-SF9009-HIST', (SELECT id FROM sales_orders WHERE order_no = 'SF9009'),
                  '包装外观异常的历史冲销（虚构）', '1200.00', '1200.00',
                  'APPROVED', 'synthetic-seed')"""
    )
    conn.execute(
        """INSERT INTO refund_executions(
            refund_request_id, idempotency_key, executed_amount,
            provider_reference, status
        ) VALUES (?, 'seed-refund-sf9009', '1200.00', 'DEMO-RF-SF9009-HIST', 'EXECUTED')""",
        (refund_request.lastrowid,),
    )
    conn.execute(
        """INSERT INTO audit_logs(actor, action, resource_type, resource_id, result)
        VALUES ('synthetic-seed', 'REFUND_EXECUTE', 'sales_order', 'SF9009', 'SUCCESS')"""
    )
def _ensure_quality_demo_data(conn: sqlite3.Connection) -> None:
    """Keep synthetic quality records available for the manufacturing demo."""
    inspector = conn.execute(
        "SELECT id FROM users WHERE username = 'quality_seed'"
    ).fetchone()
    if inspector is None:
        inspector_id = conn.execute(
            """INSERT INTO users(username, display_name, password_hash, role, department)
            VALUES ('quality_seed', '质检演示班组', 'synthetic-seed', 'employee', '质量部')"""
        ).lastrowid
    else:
        inspector_id = inspector["id"]
    if conn.execute("SELECT 1 FROM quality_reports LIMIT 1").fetchone():
        return
    rows = [
        ("SF1001", 1000, 980, 20, "2026-08-25"),
        ("SF2002", 300, 295, 5, "2026-08-25"),
        ("SF3003", 800, 792, 8, "2026-08-25"),
        ("SF5005", 400, 390, 10, "2026-08-25"),
        ("SF8008", 160, 156, 4, "2026-08-25"),
    ]
    conn.executemany(
        """INSERT INTO quality_reports(
            order_id, inspected_quantity, qualified_quantity, rejected_quantity,
            inspector_id, report_date, remark
        ) VALUES ((SELECT id FROM sales_orders WHERE order_no = ?), ?, ?, ?, ?, ?, '虚构演示质检数据')""",
        [(order_no, inspected, qualified, rejected, inspector_id, report_date) for order_no, inspected, qualified, rejected, report_date in rows],
    )
def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO customers(customer_code, name, contact_name, phone, address) VALUES (?, ?, ?, ?, ?)",
        CUSTOMERS,
    )
    conn.executemany(
        "INSERT INTO products(sku, product_name, material, product_type) VALUES (?, ?, ?, ?)",
        PRODUCTS,
    )
    product_ids = {
        row["sku"]: row["id"]
        for row in conn.execute("SELECT id, sku FROM products")
    }
    conn.executemany(
        """INSERT INTO product_specs(
            product_id, version, reel_diameter, reel_width, core_diameter,
            flange_diameter, color, antistatic, cleanliness_requirement,
            effective_from
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-01-01')""",
        [(product_ids[sku], version, diameter, width, core, flange, color, esd, clean)
         for sku, version, diameter, width, core, flange, color, esd, clean in PRODUCT_SPECS],
    )
    conn.executemany(
        "INSERT INTO warehouses(warehouse_code, name, location) VALUES (?, ?, ?)",
        [("WH-SZ-01", "深圳成品仓", "深圳宝安"), ("WH-DG-01", "东莞周转仓", "东莞松山湖")],
    )
    customer_ids = {
        row["customer_code"]: row["id"]
        for row in conn.execute("SELECT id, customer_code FROM customers")
    }
    conn.executemany(
        """INSERT INTO sales_orders(
            order_no, customer_id, status, order_date, required_date,
            total_amount, created_by
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(number, customer_ids[customer], status, order_date, required_date, total, created_by)
         for number, customer, status, order_date, required_date, total, created_by in ORDERS],
    )
    order_ids = {
        row["order_no"]: row["id"]
        for row in conn.execute("SELECT id, order_no FROM sales_orders")
    }
    for order_no, sku, quantity, price, completed, shipped, cancelled in ORDER_LINES:
        product_id = product_ids[sku]
        spec = conn.execute(
            "SELECT * FROM product_specs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product_id,),
        ).fetchone()
        conn.execute(
            """INSERT INTO sales_order_lines(
                order_id, product_id, product_spec_snapshot, ordered_quantity,
                unit_price, completed_quantity, shipped_quantity, cancelled_quantity
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                order_ids[order_no], product_id, str(dict(spec)), quantity, price,
                completed, shipped, cancelled,
            ),
        )
    line_ids = {
        row["order_no"]: row["id"]
        for row in conn.execute(
            """SELECT sol.id, so.order_no FROM sales_order_lines sol
            JOIN sales_orders so ON so.id = sol.order_id"""
        )
    }
    production_seed = [
        ("MO-SF1001", "SF1001", 1000, "COMPLETED", 1000, 980, 20),
        ("MO-SF2002", "SF2002", 500, "IN_PROGRESS", 320, 315, 5),
        ("MO-SF3003", "SF3003", 800, "COMPLETED", 800, 792, 8),
        ("MO-SF5005", "SF5005", 1000, "IN_PROGRESS", 420, 410, 10),
        ("MO-SF8008", "SF8008", 500, "IN_PROGRESS", 180, 176, 4),
    ]
    for production_no, order_no, planned, status, reported, qualified, rejected in production_seed:
        cursor = conn.execute(
            """INSERT INTO production_orders(
                production_no, order_line_id, planned_quantity, status,
                planned_start_at, planned_end_at
            ) VALUES (?, ?, ?, ?, '2026-08-01', '2026-09-30')""",
            (production_no, line_ids[order_no], planned, status),
        )
        conn.execute(
            """INSERT INTO production_reports(
                production_order_id, report_date, reported_quantity,
                qualified_quantity, rejected_quantity, operator_name, remark
            ) VALUES (?, '2026-08-25', ?, ?, ?, '生产班组A', '虚构演示报工数据')""",
            (cursor.lastrowid, reported, qualified, rejected),
        )
    warehouse_ids = {
        row["warehouse_code"]: row["id"]
        for row in conn.execute("SELECT id, warehouse_code FROM warehouses")
    }
    inventory_seed = [
        ("REEL-7IN-BLACK", "WH-SZ-01", "LOT-260801", 2600, 300),
        ("REEL-7IN-CLEAR", "WH-SZ-01", "LOT-260802", 1200, 600),
        ("REEL-13IN-BLACK", "WH-SZ-01", "LOT-260803", 780, 500),
        ("REEL-13IN-CLEAR", "WH-DG-01", "LOT-260804", 1550, 1000),
        ("REEL-15IN-ESD", "WH-SZ-01", "LOT-260805", 420, 100),
        ("REEL-22IN-ESD", "WH-DG-01", "LOT-260806", 260, 150),
        ("REEL-5IN-BLACK", "WH-SZ-01", "LOT-260807", 900, 200),
        ("REEL-7IN-WHITE", "WH-DG-01", "LOT-260808", 480, 0),
    ]
    for sku, warehouse, lot, on_hand, reserved in inventory_seed:
        conn.execute(
            """INSERT INTO inventory_balances(
                product_id, warehouse_id, lot_no, on_hand_quantity, reserved_quantity
            ) VALUES (?, ?, ?, ?, ?)""",
            (product_ids[sku], warehouse_ids[warehouse], lot, on_hand, reserved),
        )
    logistics_seed = [
        ("SF1001", "DELIVERED", "深圳封装厂收货区", "2026-08-14 10:30:00"),
        ("SF2002", "IN_PRODUCTION", "深圳生产车间", "2026-08-25 09:00:00"),
        ("SF3003", "DELIVERED", "苏州封测厂收货区", "2026-08-20 15:20:00"),
        ("SF5005", "PARTIAL_SHIPMENT", "深圳成品仓", "2026-08-24 16:00:00"),
        ("SF9009", "IN_TRANSIT", "广州转运中心", "2026-08-25 13:00:00"),
        ("9527", "IN_TRANSIT", "深圳成品仓", "2026-07-08 12:00:00"),
        ("1001", "READY_TO_SHIP", "深圳成品仓", "2026-07-09 12:00:00"),
    ]
    conn.executemany(
        """INSERT INTO logistics_events(order_id, status, location, event_time)
        VALUES ((SELECT id FROM sales_orders WHERE order_no = ?), ?, ?, ?)""",
        logistics_seed,
    )
    # One historical partial refund makes the demo able to show the
    # difference between order total and current refundable amount.
    refund_request = conn.execute(
        """INSERT INTO refund_requests(
            refund_no, order_id, reason, requested_amount,
            approved_amount, status, requested_by
        ) VALUES ('RF-SF9009-HIST', (SELECT id FROM sales_orders WHERE order_no = 'SF9009'),
                  '包装外观异常的历史冲销（虚构）', '1200.00', '1200.00',
                  'APPROVED', 'synthetic-seed')"""
    )
    conn.execute(
        """INSERT INTO refund_executions(
            refund_request_id, idempotency_key, executed_amount,
            provider_reference, status
        ) VALUES (?, 'seed-refund-sf9009', '1200.00', 'DEMO-RF-SF9009-HIST', 'EXECUTED')""",
        (refund_request.lastrowid,),
    )
    conn.execute(
        """INSERT INTO audit_logs(actor, action, resource_type, resource_id, result)
        VALUES ('synthetic-seed', 'REFUND_EXECUTE', 'sales_order', 'SF9009', 'SUCCESS')"""
    )
    _ensure_quality_demo_data(conn)


def get_order(order_no: str) -> dict | None:
    initialize_database()
    with connection() as conn:
        row = conn.execute(
            """SELECT so.order_no, so.status, so.total_amount, so.order_date,
                so.required_date, c.customer_code, c.name AS customer_name,
                GROUP_CONCAT(p.product_name, '；') AS products,
                COALESCE(SUM(sol.ordered_quantity), 0) AS ordered_quantity,
                COALESCE(SUM(sol.completed_quantity), 0) AS completed_quantity,
                COALESCE(SUM(sol.shipped_quantity), 0) AS shipped_quantity
            FROM sales_orders so
            JOIN customers c ON c.id = so.customer_id
            LEFT JOIN sales_order_lines sol ON sol.order_id = so.id
            LEFT JOIN products p ON p.id = sol.product_id
            WHERE so.order_no = ?
            GROUP BY so.id""",
            (order_no,),
        ).fetchone()
        if row is None:
            return None
        refunded = conn.execute(
            """SELECT COALESCE(SUM(re.executed_amount), 0) AS amount
            FROM refund_executions re
            JOIN refund_requests rr ON rr.id = re.refund_request_id
            JOIN sales_orders so ON so.id = rr.order_id
            WHERE so.order_no = ? AND re.status = 'EXECUTED'""",
            (order_no,),
        ).fetchone()["amount"]
        data = dict(row)
        data.update(
            {
                "order_id": data.pop("order_no"),
                "product": data.pop("products") or "塑胶载体卷轴",
                "amount": float(Decimal(str(data.pop("total_amount") or "0"))),
                "refunded_amount": float(Decimal(str(refunded or "0"))),
                "refundable_amount": float(
                    max(Decimal("0"), Decimal(str(row["total_amount"])) - Decimal(str(refunded or "0")))
                ),
            }
        )
        return data


def get_logistics(order_no: str) -> dict | None:
    initialize_database()
    with connection() as conn:
        row = conn.execute(
            """SELECT so.order_no, le.status, le.location, le.event_time
            FROM logistics_events le
            JOIN sales_orders so ON so.id = le.order_id
            WHERE so.order_no = ? ORDER BY le.id DESC LIMIT 1""",
            (order_no,),
        ).fetchone()
        return dict(row) | {"order_id": order_no} if row else None


def get_inventory(*, sku: str | None = None, keyword: str | None = None) -> list[dict]:
    initialize_database()
    with connection() as conn:
        clauses: list[str] = []
        params: list[str] = []
        if sku:
            clauses.append("p.sku = ?")
            params.append(sku)
        elif keyword:
            clauses.append("(p.sku LIKE ? OR p.product_name LIKE ? OR p.material LIKE ?)")
            value = f"%{keyword}%"
            params.extend([value, value, value])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""SELECT p.sku, p.product_name, w.warehouse_code, w.name AS warehouse_name,
                ib.lot_no, ib.on_hand_quantity, ib.reserved_quantity,
                ib.on_hand_quantity - ib.reserved_quantity AS available_quantity
            FROM inventory_balances ib
            JOIN products p ON p.id = ib.product_id
            JOIN warehouses w ON w.id = ib.warehouse_id
            {where} ORDER BY p.sku, w.warehouse_code""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def get_production(*, order_no: str | None = None, production_no: str | None = None) -> list[dict]:
    initialize_database()
    with connection() as conn:
        clauses: list[str] = []
        params: list[str] = []
        if order_no:
            clauses.append("so.order_no = ?")
            params.append(order_no)
        if production_no:
            clauses.append("po.production_no = ?")
            params.append(production_no)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""SELECT po.production_no, so.order_no, p.sku, p.product_name,
                po.planned_quantity, po.status,
                COALESCE(SUM(pr.reported_quantity), 0) AS reported_quantity,
                COALESCE(SUM(pr.qualified_quantity), 0) AS qualified_quantity,
                COALESCE(SUM(pr.rejected_quantity), 0) AS rejected_quantity
            FROM production_orders po
            JOIN sales_order_lines sol ON sol.id = po.order_line_id
            JOIN sales_orders so ON so.id = sol.order_id
            JOIN products p ON p.id = sol.product_id
            LEFT JOIN production_reports pr ON pr.production_order_id = po.id AND pr.status = 'ACTIVE'
            {where} GROUP BY po.id ORDER BY po.production_no""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]


def get_quality_reports(order_no: str | None = None) -> list[dict]:
    initialize_database()
    with connection() as conn:
        params: list[str] = []
        clause = "WHERE 1 = 1"
        if order_no:
            clause += " AND so.order_no = ?"
            params.append(order_no)
        rows = conn.execute(
            f"""SELECT so.order_no, p.sku, qr.inspected_quantity,
                qr.qualified_quantity, qr.rejected_quantity, qr.report_date,
                qr.remark, 'ACTIVE' AS status
            FROM quality_reports qr JOIN sales_orders so ON so.id = qr.order_id
            LEFT JOIN sales_order_lines sol ON sol.order_id = so.id
            LEFT JOIN products p ON p.id = sol.product_id {clause}
            ORDER BY qr.report_date DESC, qr.id DESC""", params
        ).fetchall()
        return [dict(row) for row in rows]


def get_refund_by_key(idempotency_key: str) -> dict | None:
    initialize_database()
    with connection() as conn:
        row = conn.execute(
            """SELECT so.order_no AS order_id, re.executed_amount AS refund_amount,
                rr.reason, re.status, re.idempotency_key
            FROM refund_executions re
            JOIN refund_requests rr ON rr.id = re.refund_request_id
            JOIN sales_orders so ON so.id = rr.order_id
            WHERE re.idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None


def execute_refund(
    *, order_no: str, amount: Decimal, reason: str, idempotency_key: str
) -> dict:
    initialize_database()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT so.order_no AS order_id, re.executed_amount AS refund_amount,
                rr.reason, re.status, re.idempotency_key
            FROM refund_executions re
            JOIN refund_requests rr ON rr.id = re.refund_request_id
            JOIN sales_orders so ON so.id = rr.order_id
            WHERE re.idempotency_key = ?""",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return dict(existing)
        order = conn.execute(
            "SELECT id, total_amount FROM sales_orders WHERE order_no = ?",
            (order_no,),
        ).fetchone()
        if order is None:
            raise LookupError("NOT_FOUND")
        refunded = conn.execute(
            """SELECT COALESCE(SUM(re.executed_amount), 0) AS amount
            FROM refund_executions re JOIN refund_requests rr ON rr.id = re.refund_request_id
            WHERE rr.order_id = ? AND re.status = 'EXECUTED'""",
            (order["id"],),
        ).fetchone()["amount"]
        refundable = max(Decimal("0"), Decimal(str(order["total_amount"])) - Decimal(str(refunded or "0")))
        if amount > refundable:
            raise ValueError(f"INVALID_REFUND_AMOUNT:{refundable:.2f}")
        refund_no = f"RF-{order_no}-{idempotency_key[-8:].upper()}"
        request_cursor = conn.execute(
            """INSERT INTO refund_requests(
                refund_no, order_id, reason, requested_amount,
                approved_amount, status, requested_by
            ) VALUES (?, ?, ?, ?, ?, 'APPROVED', 'supportflow-demo')""",
            (refund_no, order["id"], reason, str(amount), str(amount)),
        )
        result = {
            "order_id": order_no,
            "refund_amount": float(amount),
            "reason": reason,
            "status": "refunded",
            "idempotency_key": idempotency_key,
        }
        conn.execute(
            """INSERT INTO refund_executions(
                refund_request_id, idempotency_key, executed_amount,
                provider_reference, status
            ) VALUES (?, ?, ?, ?, 'EXECUTED')""",
            (request_cursor.lastrowid, idempotency_key, str(amount), f"DEMO-{refund_no}"),
        )
        conn.execute(
            """INSERT INTO audit_logs(actor, action, resource_type, resource_id, result)
            VALUES ('supportflow-demo', 'REFUND_EXECUTE', 'sales_order', ?, ?)""",
            (order_no, "SUCCESS"),
        )
        return result


def create_sales_order(*, customer_code: str, sku: str, quantity: int, required_date: str) -> dict:
    initialize_database()
    with connection() as conn:
        customer = conn.execute(
            "SELECT id, name FROM customers WHERE customer_code = ? AND status = 'ACTIVE'",
            (customer_code,),
        ).fetchone()
        product = conn.execute(
            "SELECT id, product_name FROM products WHERE sku = ? AND status = 'ACTIVE'",
            (sku,),
        ).fetchone()
        if customer is None:
            raise LookupError("CUSTOMER_NOT_FOUND")
        if product is None:
            raise LookupError("PRODUCT_NOT_FOUND")
        order_no = f"SF-NEW-{conn.execute('SELECT COALESCE(MAX(id), 0) + 1 FROM sales_orders').fetchone()[0]:04d}"
        unit_price = {
            "REEL-7IN-BLACK": Decimal("18.00"),
            "REEL-7IN-CLEAR": Decimal("21.00"),
            "REEL-13IN-BLACK": Decimal("55.00"),
            "REEL-13IN-CLEAR": Decimal("33.00"),
            "REEL-15IN-ESD": Decimal("52.50"),
            "REEL-22IN-ESD": Decimal("103.00"),
            "REEL-5IN-BLACK": Decimal("12.00"),
            "REEL-7IN-WHITE": Decimal("22.00"),
        }.get(sku, Decimal("0"))
        total = unit_price * quantity
        cursor = conn.execute(
            """INSERT INTO sales_orders(
                order_no, customer_id, status, order_date, required_date,
                total_amount, created_by
            ) VALUES (?, ?, 'SUBMITTED', DATE('now'), ?, ?, 'supportflow-demo')""",
            (order_no, customer["id"], required_date, str(total)),
        )
        spec = conn.execute(
            "SELECT * FROM product_specs WHERE product_id = ? ORDER BY id DESC LIMIT 1",
            (product["id"],),
        ).fetchone()
        conn.execute(
            """INSERT INTO sales_order_lines(
                order_id, product_id, product_spec_snapshot, ordered_quantity, unit_price
            ) VALUES (?, ?, ?, ?, ?)""",
            (cursor.lastrowid, product["id"], str(dict(spec)), quantity, str(unit_price)),
        )
        return {
            "order_id": order_no,
            "customer_code": customer_code,
            "customer_name": customer["name"],
            "sku": sku,
            "product": product["product_name"],
            "quantity": quantity,
            "required_date": required_date,
            "total_amount": float(total),
            "status": "SUBMITTED",
        }


initialize_database()
