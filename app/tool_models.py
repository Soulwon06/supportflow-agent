from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ToolResult:
    success: bool
    data: Optional[Any] = None
    error_code: Optional[str] = None
    error_message: str = ""
    retryable: bool = False


def success_result(data: Any) -> ToolResult:
    return ToolResult(
        success=True,
        data=data,
        error_code=None,
        error_message="",
        retryable=False,
    )


def error_result(
    error_code: str,
    error_message: str,
    retryable: bool = False,
) -> ToolResult:
    return ToolResult(
        success=False,
        data=None,
        error_code=error_code,
        error_message=error_message,
        retryable=retryable,
    )