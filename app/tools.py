import time
from decimal import Decimal, InvalidOperation
import os

from .tool_models import ToolResult, success_result, error_result
from .database import (
    create_sales_order,
    execute_refund,
    get_inventory,
    get_logistics,
    get_order,
    get_production,
    get_quality_reports,
    get_refund_by_key,
)

ALLOWED_TOOLS = {
    "query_order",
    "query_logistics",
    "refund_order",
    "query_inventory",
    "query_production",
    "create_sales_order",
}
# =========================
# Mock Data
# =========================

MOCK_ORDERS = {
    "9527": {
        "order_id": "9527",
        "status": "shipped",
        "product": "机械键盘",
        "amount": 500.0,
        "ordered_quantity": 25,
        "completed_quantity": 25,
    },
    "1001": {
        "order_id": "1001",
        "status": "paid",
        "product": "无线鼠标",
        "amount": 199.0,
        "ordered_quantity": 10,
        "completed_quantity": 0,
    },
    "SF1001": {
        "order_id": "SF1001",
        "status": "shipped",
        "product": "机械键盘",
        "amount": 500.0,
        "ordered_quantity": 1000,
        "completed_quantity": 1000,
    },
    "SF2002": {
        "order_id": "SF2002",
        "status": "paid",
        "product": "无线耳机",
        "amount": 299.0,
        "ordered_quantity": 500,
        "completed_quantity": 320,
    },
}


MOCK_LOGISTICS = {
    "9527": {
        "order_id": "9527",
        "status": "运输中",
        "location": "广州转运中心",
    },
    "SF1001": {
        "order_id": "SF1001",
        "status": "运输中",
        "location": "深圳转运中心",
    },
    "SF2002": {
        "order_id": "SF2002",
        "status": "已揽收",
        "location": "上海分拨中心",
    },
}


# 模拟服务端保存已经处理过的退款请求
PROCESSED_REFUNDS = {}
REFUNDED_AMOUNTS: dict[str, Decimal] = {}


def _test_mode() -> bool:
    """Keep existing unit-test reset hooks out of the durable demo database."""

    return os.getenv("SUPPORTFLOW_TEST_MODE") == "1"


def current_refundable_amount(order_id: str) -> Decimal:
    if _test_mode():
        order = MOCK_ORDERS.get(order_id)
        if order is None:
            return Decimal("0")
        total = Decimal(str(order["amount"]))
        refunded = REFUNDED_AMOUNTS.get(order_id, Decimal("0"))
        return max(Decimal("0"), total - refunded)
    order = get_order(order_id)
    return Decimal(str(order.get("refundable_amount", "0"))) if order else Decimal("0")


# =========================
# Order Tool
# =========================

def query_order(order_id: str) -> ToolResult:
    if not isinstance(order_id, str):
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="order_id 必须是字符串",
            retryable=False,
        )

    if not order_id.strip():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="order_id 不能为空",
            retryable=False,
        )

    if _test_mode():
        order = MOCK_ORDERS.get(order_id)
        if order is None:
            return error_result(
                error_code="NOT_FOUND",
                error_message=f"未找到订单 {order_id}",
                retryable=False,
            )
        trusted_order = dict(order)
        trusted_order["refundable_amount"] = float(current_refundable_amount(order_id))
        return success_result(trusted_order)

    order = get_order(order_id)

    if order is None:
        return error_result(
            error_code="NOT_FOUND",
            error_message=f"未找到订单 {order_id}",
            retryable=False,
        )

    return success_result(dict(order))


# =========================
# Logistics Tool
# =========================

def query_logistics(
    order_id: str,
    simulate_failure: bool = False,
) -> ToolResult:

    if not isinstance(order_id, str):
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="order_id 必须是字符串",
            retryable=False,
        )

    if not order_id.strip():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="order_id 不能为空",
            retryable=False,
        )

    max_retries = 3

    for attempt in range(1, max_retries + 1):
        try:
            print(f"物流查询：第 {attempt} 次尝试")

            if simulate_failure:
                raise TimeoutError("Logistics service timeout")

            if attempt < 3:
                raise TimeoutError("Logistics service timeout")

            logistics = get_logistics(order_id)

            if logistics is None:
                return error_result(
                    error_code="NOT_FOUND",
                    error_message=f"未找到订单 {order_id} 的物流信息",
                    retryable=False,
                )

            return success_result(logistics)

        except TimeoutError as e:
            print(f"物流查询超时：{e}")

            if attempt == max_retries:
                return error_result(
                    error_code="TIMEOUT",
                    error_message="物流服务多次请求超时",
                    retryable=True,
                )

            wait_time = 0.5 * (2 ** (attempt - 1))
            print(f"等待 {wait_time} 秒后重试...")
            time.sleep(wait_time)

    return error_result(
        error_code="INTERNAL_ERROR",
        error_message="未知物流查询错误",
        retryable=False,
    )


# =========================
# Refund Tool
# =========================

def refund_order(
    order_id: str,
    amount: float,
    reason: str,
    idempotency_key: str,
    confirmed: bool = False,
) -> ToolResult:

    # 1. Validation
    if not isinstance(order_id, str) or not order_id.strip():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="order_id 必须是非空字符串",
            retryable=False,
        )

    if isinstance(amount, bool):
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="amount 必须是有效的数字",
            retryable=False,
        )

    try:
        amount_decimal = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="amount 必须是有效的数字",
            retryable=False,
        )

    if not amount_decimal.is_finite():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="amount 必须是有限数字",
            retryable=False,
        )

    if amount_decimal.as_tuple().exponent < -2:
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="退款金额最多保留两位小数",
            retryable=False,
        )

    if amount_decimal <= 0:
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="amount 必须是大于 0 的数字",
            retryable=False,
        )

    if not isinstance(reason, str) or not reason.strip():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="reason 不能为空",
            retryable=False,
        )

    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="idempotency_key 不能为空",
            retryable=False,
        )

    # 2. Human Confirmation
    if confirmed is not True:
        return error_result(
            error_code="CONFIRMATION_REQUIRED",
            error_message="退款属于敏感操作，需要用户确认",
            retryable=False,
        )

    # 3. Idempotency Check
    if _test_mode():
        if idempotency_key in PROCESSED_REFUNDS:
            return success_result(PROCESSED_REFUNDS[idempotency_key])
    else:
        existing = get_refund_by_key(idempotency_key)
        if existing:
            return success_result(existing)

    # 4. Execute-time business revalidation from trusted backend data.
    order = MOCK_ORDERS.get(order_id) if _test_mode() else get_order(order_id)

    if order is None:
        return error_result(
            error_code="NOT_FOUND",
            error_message=f"未找到订单 {order_id}",
            retryable=False,
        )

    refundable_amount = current_refundable_amount(order_id)
    if amount_decimal > refundable_amount:
        return error_result(
            error_code="INVALID_REFUND_AMOUNT",
            error_message=(
                f"退款金额不能超过当前可退款金额 ¥{refundable_amount:.2f}"
            ),
            retryable=False,
        )

    # 5. Execute Refund
    if not _test_mode():
        try:
            return success_result(
                execute_refund(
                    order_no=order_id,
                    amount=amount_decimal,
                    reason=reason,
                    idempotency_key=idempotency_key,
                )
            )
        except LookupError:
            return error_result(
                error_code="NOT_FOUND",
                error_message=f"未找到订单 {order_id}",
                retryable=False,
            )
        except ValueError as exc:
            detail = str(exc)
            if detail.startswith("INVALID_REFUND_AMOUNT:"):
                refundable = detail.split(":", 1)[1]
                return error_result(
                    error_code="INVALID_REFUND_AMOUNT",
                    error_message=f"退款金额不能超过当前可退款金额 ¥{refundable}",
                    retryable=False,
                )
            raise

    refund_data = {
        "order_id": order_id,
        "refund_amount": float(amount_decimal),
        "reason": reason,
        "status": "refunded",
        "idempotency_key": idempotency_key,
    }

    # 模拟服务端持久化退款结果
    PROCESSED_REFUNDS[idempotency_key] = refund_data
    REFUNDED_AMOUNTS[order_id] = (
        REFUNDED_AMOUNTS.get(order_id, Decimal("0")) + amount_decimal
    )

    return success_result(refund_data)


def query_inventory(
    sku: str | None = None,
    keyword: str | None = None,
) -> ToolResult:
    """Return trusted inventory balances from the synthetic database."""

    rows = get_inventory(sku=sku, keyword=keyword)
    if not rows:
        return error_result(
            error_code="NOT_FOUND",
            error_message="没有找到匹配的库存资料",
            retryable=False,
        )
    return success_result({"items": rows})


def query_production(
    order_id: str | None = None,
    production_no: str | None = None,
) -> ToolResult:
    rows = get_production(order_no=order_id, production_no=production_no)
    if not rows:
        return error_result(
            error_code="NOT_FOUND",
            error_message="没有找到匹配的生产任务",
            retryable=False,
        )
    return success_result({"items": rows})


def query_quality(order_id: str | None = None) -> ToolResult:
    rows = get_quality_reports(order_no=order_id)
    if not rows:
        return error_result(error_code="NOT_FOUND", error_message="没有找到匹配的质检记录", retryable=False)
    return success_result({"items": rows})


def submit_sales_order(
    customer_code: str,
    sku: str,
    quantity: int,
    required_date: str,
) -> ToolResult:
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        return error_result(
            error_code="INVALID_ARGUMENT",
            error_message="下单数量必须是大于 0 的整数",
            retryable=False,
        )
    try:
        return success_result(
            create_sales_order(
                customer_code=customer_code,
                sku=sku,
                quantity=quantity,
                required_date=required_date,
            )
        )
    except LookupError as exc:
        error_code = str(exc)
        message = {
            "CUSTOMER_NOT_FOUND": f"未找到客户 {customer_code}",
            "PRODUCT_NOT_FOUND": f"未找到产品 {sku}",
        }.get(error_code, "下单资料不存在")
        return error_result(error_code=error_code, error_message=message, retryable=False)


# =========================
# Tests
# =========================

if __name__ == "__main__":
    print("=== Refund Case 1：未确认 ===")
    print(
        refund_order(
            order_id="9527",
            amount=500,
            reason="商品质量问题",
            idempotency_key="refund-9527-001",
            confirmed=False,
        )
    )

    print("\n=== Refund Case 2：用户确认，第一次退款 ===")
    print(
        refund_order(
            order_id="9527",
            amount=500,
            reason="商品质量问题",
            idempotency_key="refund-9527-001",
            confirmed=True,
        )
    )

    print("\n=== Refund Case 3：相同请求再次到达 ===")
    print(
        refund_order(
            order_id="9527",
            amount=500,
            reason="商品质量问题",
            idempotency_key="refund-9527-001",
            confirmed=True,
        )
    )

    print("\n=== Refund Case 4：非法退款金额 ===")
    print(
        refund_order(
            order_id="9527",
            amount=600,
            reason="商品质量问题",
            idempotency_key="refund-9527-002",
            confirmed=True,
        )
    )
