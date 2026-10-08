from app.tool_dispatcher import dispatch_tool


print("\n=== Case 1: 普通客服查询订单 ===")

result = dispatch_tool(
    "query_order",
    role="customer_service",
    order_id="9527",
)

print(result)


print("\n=== Case 2: 普通客服申请退款 ===")

result = dispatch_tool(
    "refund_order",
    role="customer_service",
    order_id="9527",
    amount=500,
    reason="用户申请退款",
    idempotency_key="permission-test-001",
    confirmed=True,
)

print(result)


print("\n=== Case 3: 主管申请退款 ===")

result = dispatch_tool(
    "refund_order",
    role="manager",
    order_id="9527",
    amount=500,
    reason="用户申请退款",
    idempotency_key="permission-test-002",
    confirmed=True,
)

print(result)


print("\n=== Case 4: 未知角色查询订单 ===")

result = dispatch_tool(
    "query_order",
    role="unknown_role",
    order_id="9527",
)

print(result)


print("\n=== Case 5: 非法 Tool ===")

result = dispatch_tool(
    "delete_database",
    role="manager",
)

print(result)