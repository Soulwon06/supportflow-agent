import time

from app.tool_dispatcher import (
    dispatch_tool,
    CIRCUIT_BREAKERS,
)


def print_circuit_state():
    circuit = CIRCUIT_BREAKERS["query_logistics"]

    print(
        f"Circuit State: {circuit.state}, "
        f"Failure Count: {circuit.failure_count}"
    )


print("\n=== 初始状态 ===")
print_circuit_state()


# ============================================================
# Case 1
# 第一次完整 Tool 调用失败
# ============================================================

print("\n=== Case 1: 第一次物流调用失败 ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
    simulate_failure=True,
)

print(result)
print_circuit_state()


# ============================================================
# Case 2
# 第二次完整 Tool 调用失败
# 达到 failure_threshold，Circuit 应进入 OPEN
# ============================================================

print("\n=== Case 2: 第二次物流调用失败 ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
    simulate_failure=True,
)

print(result)
print_circuit_state()


# ============================================================
# Case 3
# Circuit 已 OPEN
# 这一次不应该真正进入 Logistics Tool
# ============================================================

print("\n=== Case 3: OPEN 状态下再次请求 ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
)

print(result)
print_circuit_state()


# ============================================================
# 等待恢复时间
# ============================================================

print("\n等待 5.5 秒，让 Circuit Breaker 进入恢复窗口...")

time.sleep(5.5)


# ============================================================
# Case 4
# OPEN 超时后允许一次试探请求
# allow_request() 会切换为 HALF_OPEN
#
# 当前正常 query_logistics：
# 前两次 Timeout，第三次成功
#
# 最终 ToolResult.success == True
# 所以 Circuit 应恢复 CLOSED
# ============================================================

print("\n=== Case 4: 恢复窗口后的试探请求 ===")

result = dispatch_tool(
    "query_logistics",
    role="customer_service",
    order_id="9527",
)

print(result)
print_circuit_state()