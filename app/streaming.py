import json
import logging
import asyncio
from typing import Any, AsyncGenerator

from .graph import app as agent_app
from .state import create_turn_input
from .observability import (
    Trace,
    reset_progress_emitter,
    set_current_trace,
    set_progress_emitter,
    reset_current_trace,
)
from .trace_payload import build_trace_payload


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# SSE Event Builder
# ============================================================

def build_sse_event(
    event: str,
    data: Any,
) -> str:
    """
    把 Python 数据转换成标准 SSE 格式。
    """

    json_data = json.dumps(
        data,
        ensure_ascii=False,
    )

    return f"event: {event}\ndata: {json_data}\n\n"


# ============================================================
# LangGraph → SSE Streaming Adapter
# ============================================================

async def stream_agent(
    message: str,
    thread_id: str,
    role: str,
    user_id: int = 0,
    permissions: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """
    运行 LangGraph Agent，
    把 Workflow Update 转换成 SSE Event。

    同时记录：
    - Trace
    - Node Span
    - Workflow Update Span
    - TTFE
    - Total Latency
    - Structured Logging
    """

    # ========================================================
    # 1. 创建当前请求的 Trace
    # ========================================================

    trace = Trace(
        metadata={
            "thread_id": thread_id,
            "role": role,
        }
    )

    # ========================================================
    # 2. 把 Trace 放进当前异步 Context
    # ========================================================

    trace_token = set_current_trace(trace)
    progress_token = None
    workflow_task = None
    progress_task = None
    workflow_event_task = None
    latest_state = {}

    try:
        config = {
            "configurable": {
                "thread_id": thread_id,
            }
        }

        # ====================================================
        # 3. Initial State
        # ====================================================

        existing_state = None
        try:
            existing_state = agent_app.get_state(config).values
        except Exception:
            existing_state = None

        initial_state = create_turn_input(
            user_message=message,
            role=role,
            user_id=user_id,
            existing_state=existing_state,
        )
        if permissions is not None:
            initial_state["permissions"] = list(permissions)

        # ====================================================
        # 4. First Event / TTFE
        # ====================================================

        trace.mark_first_event()

        yield build_sse_event(
            "start",
            {
                "message": "SupportFlow Agent 开始处理请求",
                "thread_id": thread_id,
                "trace_id": trace.trace_id,
            },
        )

        try:
            # ================================================
            # 5. Real LangGraph Workflow Streaming
            # ================================================

            workflow_queue: asyncio.Queue = asyncio.Queue()
            progress_queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def enqueue_progress(event: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(progress_queue.put_nowait, event)

            progress_token = set_progress_emitter(enqueue_progress)

            async def run_workflow() -> None:
                try:
                    async for update in agent_app.astream(
                        initial_state,
                        config=config,
                        stream_mode="updates",
                    ):
                        await workflow_queue.put(("update", update))
                except BaseException as exc:
                    await workflow_queue.put(("error", exc))
                finally:
                    await workflow_queue.put(("done", None))

            workflow_task = asyncio.create_task(run_workflow())
            progress_task = asyncio.create_task(progress_queue.get())
            workflow_event_task = asyncio.create_task(workflow_queue.get())
            workflow_done = False

            while not workflow_done:
                pending = {progress_task, workflow_event_task}
                completed, _ = await asyncio.wait(
                    pending,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if progress_task in completed:
                    yield build_sse_event("progress", progress_task.result())
                    progress_task = asyncio.create_task(progress_queue.get())

                if workflow_event_task not in completed:
                    continue

                event_kind, event_value = workflow_event_task.result()
                if event_kind == "done":
                    workflow_done = True
                    continue
                if event_kind == "error":
                    raise event_value

                update = event_value
                # --------------------------------------------
                # 这里记录的是 Streaming Adapter
                # 处理一次 Workflow Update 的耗时。
                #
                # Node 自己的真实执行耗时，
                # 由 instrumentation.py 中的
                # traced_node() 记录。
                # --------------------------------------------

                update_span = trace.start_span(
                    "workflow_update"
                )

                try:
                    for node_name, state_update in update.items():

                        if isinstance(state_update, dict):
                            latest_state.update(state_update)

                        update_span.metadata["node"] = node_name

                        # ====================================
                        # Node Event
                        # ====================================

                        yield build_sse_event(
                            "node",
                            {
                                "node": node_name,
                                "status": "completed",
                            },
                        )

                        if not isinstance(state_update, dict):
                            continue

                        # ====================================
                        # Route Event
                        # ====================================

                        intent = state_update.get("intent")

                        if intent:
                            update_span.metadata["intent"] = intent

                            yield build_sse_event(
                                "route",
                                {
                                    "intent": intent,
                                    "source": state_update.get("routing_source", ""),
                                    "error_classification": state_update.get(
                                        "routing_error_classification", ""
                                    ),
                                },
                            )

                        # ====================================
                        # Result Event
                        # ====================================

                        result = state_update.get("result")

                        if result:
                            yield build_sse_event(
                                "result",
                                {
                                    "result": result,
                                },
                            )

                        if any(
                            key in state_update
                            for key in (
                                "answerability_status",
                                "retrieved_evidence_id",
                                "generation_skipped",
                                "safe_fallback_reason",
                            )
                        ):
                            yield build_sse_event(
                                "rag",
                                {
                                    "evidence_id": state_update.get("retrieved_evidence_id"),
                                    "evidence_text": state_update.get("retrieved_evidence_text"),
                                    "evidence_title": state_update.get("retrieved_evidence_title"),
                                    "evidence_category": state_update.get("retrieved_evidence_category"),
                                    "evidence_version": state_update.get("retrieved_evidence_version"),
                                    "rerank_score": state_update.get("rerank_score"),
                                    "answerability_status": state_update.get("answerability_status"),
                                    "generation_skipped": state_update.get("generation_skipped", False),
                                    "safe_fallback_reason": state_update.get("safe_fallback_reason", ""),
                                    "rag_source": state_update.get("rag_source", ""),
                                },
                            )

                        # ====================================
                        # Agent Business Error
                        # ====================================

                        error_log = state_update.get("error_log")

                        if error_log:
                            update_span.finish(
                                success=False,
                                error=error_log,
                            )

                            yield build_sse_event(
                                "error",
                                {
                                    "error_code": "AGENT_ERROR",
                                    "message": state_update.get("result")
                                    or "处理失败，请稍后重试。",
                                    "user_message": state_update.get("result")
                                    or "处理失败，请稍后重试。",
                                    "terminal_status": state_update.get(
                                        "terminal_status"
                                    )
                                    or "FAILED",
                                },
                            )

                            continue

                    # ========================================
                    # Workflow Update 正常完成
                    # ========================================

                    if update_span.end_time is None:
                        update_span.finish(
                            success=True
                        )

                except Exception as exc:
                    update_span.finish(
                        success=False,
                        error=str(exc),
                    )

                    raise

                workflow_event_task = asyncio.create_task(workflow_queue.get())

            if progress_task is not None:
                progress_task.cancel()
            if workflow_event_task is not None:
                workflow_event_task.cancel()
            if workflow_task is not None:
                await workflow_task

            # ================================================
            # 6. 整个 Workflow 正常结束
            # ================================================

            terminal_status = latest_state.get("terminal_status") or "SUCCESS"
            trace.finish(
                success=terminal_status not in {
                    "FAILED",
                    "CANCELLED",
                    "PERMISSION_DENIED",
                    "SAFE_FALLBACK",
                }
            )

            if (
                latest_state.get("pending_action") == "refund"
                and "amount" in (latest_state.get("missing_fields") or [])
            ):
                yield build_sse_event(
                    "refund_amount",
                    {
                        "message": "请填写退款金额，可选择全部退款。",
                        "order_id": latest_state.get("order_id")
                        or latest_state.get("current_order_id"),
                        "order_total": latest_state.get("order_total"),
                        "refundable_amount": latest_state.get("refundable_amount"),
                    },
                )
            elif latest_state.get("need_confirmation"):
                order_id = latest_state.get("order_id") or latest_state.get("current_order_id") or ""
                amount = float(latest_state.get("refund_amount") or 0.0)
                yield build_sse_event(
                    "hitl",
                    {
                        "message": f"退款属于敏感操作，需要用户确认。准备为订单 {order_id} 退款 ¥{amount:.2f}。",
                        "order_id": order_id,
                        "amount": amount,
                        "reason": latest_state.get("refund_reason"),
                    },
                )

            if terminal_status in {
                "PENDING_APPROVAL",
                "NEED_USER_INPUT",
                "FAILED",
                "CANCELLED",
                "PERMISSION_DENIED",
                "SAFE_FALLBACK",
            }:
                yield build_sse_event(
                    "terminal",
                    {
                        "status": terminal_status,
                        "message": latest_state.get("result") or "处理已结束。",
                    },
                )

            # ================================================
            # 7. Structured Summary Log
            # ================================================

            logger.info(
                "Agent request completed",
                extra={
                    "event": "agent_request_completed",
                    "trace_id": trace.trace_id,
                    "thread_id": thread_id,
                    "success": True,
                    "total_latency_ms": round(
                        trace.latency_ms or 0,
                        2,
                    ),
                    "ttfe_ms": round(
                        trace.ttfe_ms or 0,
                        2,
                    ),
                },
            )

            # ================================================
            # 8. Metrics Event
            # ================================================

            yield build_sse_event(
                "metrics",
                {
                    "trace_id": trace.trace_id,

                    "ttfe_ms": round(
                        trace.ttfe_ms or 0,
                        2,
                    ),

                    "total_latency_ms": round(
                        trace.latency_ms or 0,
                        2,
                    ),

                    "span_count": len(trace.spans),

                    "spans": [
                        {
                            "name": span.name,
                            "node": span.metadata.get("node"),
                            "provider": span.metadata.get("provider"),
                            "model": span.metadata.get("model"),
                            "operation": span.metadata.get("operation"),
                            "error_classification": span.metadata.get(
                                "error_classification"
                            ),
                            "usage": span.metadata.get("usage"),
                            "latency_ms": round(
                                span.latency_ms or 0,
                                2,
                            ),
                            "success": span.success,
                        }
                        for span in trace.spans
                    ],

                    "llm_usage": trace.llm_usage,
                },
            )

            yield build_sse_event(
                "trace",
                {
                    "trace": build_trace_payload(trace, latest_state),
                },
            )

            # ================================================
            # 9. Done Event
            # ================================================

            yield build_sse_event(
                "done",
                {
                    "message": "SupportFlow Agent 处理完成",
                    "trace_id": trace.trace_id,
                },
            )

        except Exception:
            # ================================================
            # 10. Workflow Unexpected Error
            # ================================================

            trace.finish(
                success=False,
                error="INTERNAL_ERROR",
            )

            logger.exception(
                "LangGraph streaming failed",
                extra={
                    "event": "agent_request_failed",
                    "trace_id": trace.trace_id,
                    "thread_id": thread_id,
                    "success": False,
                    "error_code": "INTERNAL_ERROR",
                    "total_latency_ms": round(
                        trace.latency_ms or 0,
                        2,
                    ),
                    "ttfe_ms": round(
                        trace.ttfe_ms or 0,
                        2,
                    ),
                },
            )

            yield build_sse_event(
                "error",
                {
                    "error_code": "INTERNAL_ERROR",
                    "message": "SupportFlow Agent internal error",
                    "trace_id": trace.trace_id,
                },
            )

    finally:
        # ====================================================
        # 11. 清理当前请求的 Trace Context
        # ====================================================

        for task in (progress_task, workflow_event_task, workflow_task):
            if task is not None and not task.done():
                task.cancel()
        if progress_token is not None:
            reset_progress_emitter(progress_token)
        reset_current_trace(trace_token)
