from .tool_models import ToolResult, error_result
from .tools import (
    query_inventory,
    query_order,
    query_logistics,
    query_production,
    query_quality,
    refund_order,
    submit_sales_order,
)
from .circuit_breaker import CircuitBreaker


ALLOWED_TOOLS = {
    "query_order",
    "query_logistics",
    "refund_order",
    "query_inventory",
    "query_production",
    "query_quality",
    "create_sales_order",
}


TOOL_REGISTRY = {
    "query_order": query_order,
    "query_logistics": query_logistics,
    "refund_order": refund_order,
    "query_inventory": query_inventory,
    "query_production": query_production,
    "query_quality": query_quality,
    "create_sales_order": submit_sales_order,
}


ROLE_PERMISSIONS = {
    "customer_service": {
        "query_order",
        "query_logistics",
        "query_inventory",
        "query_production",
        "query_quality",
    },
    "manager": {
        "query_order",
        "query_logistics",
        "refund_order",
        "query_inventory",
        "query_production",
        "query_quality",
        "create_sales_order",
    },
}


def has_tool_permission(role: str, tool_name: str, permissions=None) -> bool:
    """Return the backend authorization decision without executing a tool."""
    if permissions:
        return tool_name in set(permissions) or (tool_name == "query_quality" and "query_production" in set(permissions))
    return tool_name in ROLE_PERMISSIONS.get(role, set())


CIRCUIT_BREAKERS = {
    "query_logistics": CircuitBreaker(
        failure_threshold=2,
        recovery_timeout=5.0,
    ),
}


# 只有真正说明外部服务不健康的错误，
# 才应该影响 Circuit Breaker。
CIRCUIT_BREAKER_ERRORS = {
    "TIMEOUT",
    "SERVICE_UNAVAILABLE",
}


def dispatch_tool(
    tool_name: str,
    role: str,
    permissions=None,
    **kwargs,
) -> ToolResult:

    # 1. Tool Whitelist
    if tool_name not in ALLOWED_TOOLS:
        return error_result(
            error_code="TOOL_NOT_ALLOWED",
            error_message=f"工具 {tool_name} 不在允许列表中",
            retryable=False,
        )

    # 2. Authorization
    if not has_tool_permission(role, tool_name, permissions=permissions):
        return error_result(
            error_code="PERMISSION_DENIED",
            error_message=(
                f"角色 {role} 无权调用工具 {tool_name}"
            ),
            retryable=False,
        )

    # 3. 找到真正的 Tool Function
    tool_function = TOOL_REGISTRY.get(tool_name)

    if tool_function is None:
        return error_result(
            error_code="TOOL_NOT_FOUND",
            error_message=f"工具 {tool_name} 未注册",
            retryable=False,
        )

    # 4. Circuit Breaker 检查
    circuit_breaker = CIRCUIT_BREAKERS.get(
        tool_name
    )

    if circuit_breaker is not None:
        if not circuit_breaker.allow_request():
            return error_result(
                error_code="SERVICE_UNAVAILABLE",
                error_message=(
                    f"工具 {tool_name} 当前已熔断，"
                    "请稍后再试"
                ),
                retryable=True,
            )

    # 5. 真正执行 Tool
    result = tool_function(**kwargs)

    # 6. 根据结果更新 Circuit Breaker
    if circuit_breaker is not None:

        if result.success:
            circuit_breaker.record_success()

        elif result.error_code in CIRCUIT_BREAKER_ERRORS:
            circuit_breaker.record_failure()

        # NOT_FOUND / INVALID_ARGUMENT 等业务错误
        # 不影响 Circuit Breaker

    return result
