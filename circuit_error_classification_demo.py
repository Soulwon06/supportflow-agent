from app.tool_dispatcher import (
    dispatch_tool,
    CIRCUIT_BREAKERS,
)


breaker = CIRCUIT_BREAKERS["query_logistics"]

# 保证测试从干净状态开始
breaker.failure_count = 0
breaker.state = "CLOSED"
breaker.opened_at = None


def show_breaker():
    print(
        f"Circuit: state={breaker.state}, "
        f"failure_count={breaker.failure_count}"
    )


print("=== 初始状态 ===")
show_breaker()


print("\n=== Case 1: NOT_FOUND ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9999",
)

print("Result:", result)
show_breaker()


print("\n=== Case 2: 第一次 TIMEOUT ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
    simulate_failure=True,
)

print("Result:", result)
show_breaker()


print("\n=== Case 3: 第二次 TIMEOUT ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
    simulate_failure=True,
)

print("Result:", result)
show_breaker()


print("\n=== Case 4: OPEN 后再次请求 ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
)

print("Result:", result)
show_breaker()