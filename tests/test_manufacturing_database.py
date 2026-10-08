from app import tools
from app.database import get_inventory, get_order, get_production, initialize_database


def test_synthetic_database_contains_manufacturing_order_data():
    order = get_order("SF2002")

    assert order is not None
    assert order["product"]
    assert order["status"] == "IN_PRODUCTION"
    assert order["ordered_quantity"] == 500
    assert order["completed_quantity"] == 320


def test_inventory_and_production_queries_are_database_backed():
    inventory = get_inventory(sku="REEL-7IN-BLACK")
    production = get_production(order_no="SF2002")

    assert inventory
    assert sum(item["available_quantity"] for item in inventory) == 2300
    assert production[0]["reported_quantity"] == 320
    assert production[0]["qualified_quantity"] == 315


def test_normal_mode_refund_is_persisted_and_idempotent(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPPORTFLOW_TEST_MODE", raising=False)
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "refund.sqlite"))
    initialize_database()

    first = tools.refund_order(
        order_id="SF1001",
        amount=100,
        reason="合成数据退款演示",
        idempotency_key="demo-refund-001",
        confirmed=True,
    )
    second = tools.refund_order(
        order_id="SF1001",
        amount=100,
        reason="合成数据退款演示",
        idempotency_key="demo-refund-001",
        confirmed=True,
    )

    assert first.success is True
    assert second.success is True
    assert second.data["idempotency_key"] == "demo-refund-001"
    assert get_order("SF1001")["refundable_amount"] == 17900.0


def test_sales_order_entry_creates_submitted_order(monkeypatch, tmp_path):
    monkeypatch.delenv("SUPPORTFLOW_TEST_MODE", raising=False)
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "order-entry.sqlite"))
    initialize_database()

    result = tools.submit_sales_order(
        customer_code="CUST-001",
        sku="REEL-7IN-BLACK",
        quantity=500,
        required_date="2026-09-30",
    )

    assert result.success is True
    assert result.data["status"] == "SUBMITTED"
    assert result.data["quantity"] == 500
