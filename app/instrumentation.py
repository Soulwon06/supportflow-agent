import inspect
from typing import Any, Callable

from .observability import get_current_trace


def traced_node(
    node_name: str,
    node_function: Callable,
) -> Callable:
    """
    给 LangGraph Node 包一层 Observability。

    不修改 Node 原有业务逻辑，
    只负责记录真实 Node 执行时间。
    """

    if inspect.iscoroutinefunction(node_function):

        async def async_wrapper(state: Any):
            trace = get_current_trace()

            if trace is None:
                return await node_function(state)

            span = trace.start_span(
                "node",
                metadata={
                    "node": node_name,
                },
            )

            try:
                result = await node_function(state)

                span.finish(
                    success=True
                )

                return result

            except Exception as exc:
                span.finish(
                    success=False,
                    error=str(exc),
                )

                raise

        return async_wrapper

    def sync_wrapper(state: Any):
        trace = get_current_trace()

        if trace is None:
            return node_function(state)

        span = trace.start_span(
            "node",
            metadata={
                "node": node_name,
            },
        )

        try:
            result = node_function(state)

            span.finish(
                success=True
            )

            return result

        except Exception as exc:
            span.finish(
                success=False,
                error=str(exc),
            )

            raise

    return sync_wrapper