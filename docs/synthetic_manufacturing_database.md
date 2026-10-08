# Synthetic semiconductor-packaging manufacturing data

SupportFlow's business tools use a local SQLite database for the portfolio
demo. All records are generated examples and do not represent any real
customer, order, price, inventory, production, or refund information.

The database is created automatically at:

```text
data/supportflow_demo.db
```

Override it with:

```text
SUPPORTFLOW_DATABASE_PATH=data/supportflow_demo.db
```

## Covered business questions

- order status, customer, product, quantity, completed quantity, and amount
- logistics status and latest location
- inventory by SKU, warehouse, and lot
- production planned/reported/qualified/rejected quantities
- synthetic sales-order submission
- refund prepare, HITL confirmation, execute-time validation, and idempotency

## Example messages

```text
查询订单 SF1001
查询 SF2002 的物流
REEL-7IN-BLACK 还有多少库存
SF2002 已完成多少个
CUST-001 下单 REEL-7IN-BLACK 500个
```

The database is a source of truth for business tool results. The LLM may
identify an intent and candidate parameters, but it cannot invent inventory,
production quantities, order status, refund amounts, or permissions.

For a real deployment, replace the repository implementation with the
company-approved ERP/MES/WMS integration. Do not import real internal data
into this synthetic demo database.
