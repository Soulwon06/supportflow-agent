import time
import uuid

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# Span
# ============================================================

@dataclass
class Span:
    """
    Span = 一段具体操作的观测记录。
    """

    name: str

    start_time: float = field(
        default_factory=time.perf_counter
    )

    end_time: Optional[float] = None

    success: bool = True
    error: Optional[str] = None

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    def finish(
        self,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """
        结束当前 Span。
        """

        self.end_time = time.perf_counter()
        self.success = success
        self.error = error

    @property
    def latency_ms(self) -> Optional[float]:
        """
        当前 Span 的执行耗时，单位毫秒。
        """

        if self.end_time is None:
            return None

        return (
            self.end_time - self.start_time
        ) * 1000


# ============================================================
# Trace
# ============================================================

@dataclass
class Trace:
    """
    Trace = 一次完整请求的执行链。
    """

    trace_id: str = field(
        default_factory=lambda: uuid.uuid4().hex
    )

    start_time: float = field(
        default_factory=time.perf_counter
    )

    first_event_time: Optional[float] = None
    end_time: Optional[float] = None

    success: bool = True
    error: Optional[str] = None

    spans: List[Span] = field(
        default_factory=list
    )

    metadata: Dict[str, Any] = field(
        default_factory=dict
    )

    llm_calls: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def record_llm_usage(self, usage: Dict[str, Any]) -> None:
        """Aggregate only usage returned by the provider; never estimate it."""
        self.llm_calls += 1
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(field_name)
            if isinstance(value, int):
                current = getattr(self, field_name)
                setattr(self, field_name, (current or 0) + value)

    @property
    def llm_usage(self) -> Dict[str, Any]:
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            # No versioned DeepSeek price table is bundled with this project.
            "cost_usd": None,
        }

    def start_span(
        self,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Span:
        """
        创建一个 Span，
        并把它加入当前 Trace。
        """

        span = Span(
            name=name,
            metadata=metadata or {},
        )

        self.spans.append(span)

        return span

    def mark_first_event(self) -> None:
        """
        记录第一个 SSE Event 产生的时间。

        只记录第一次。
        """

        if self.first_event_time is None:
            self.first_event_time = time.perf_counter()

    def finish(
        self,
        success: bool = True,
        error: Optional[str] = None,
    ) -> None:
        """
        结束整个 Trace。
        """

        self.end_time = time.perf_counter()
        self.success = success
        self.error = error

    @property
    def ttfe_ms(self) -> Optional[float]:
        """
        TTFE = Time To First Event。
        """

        if self.first_event_time is None:
            return None

        return (
            self.first_event_time - self.start_time
        ) * 1000

    @property
    def latency_ms(self) -> Optional[float]:
        """
        整个 Trace 的总耗时，单位毫秒。
        """

        if self.end_time is None:
            return None

        return (
            self.end_time - self.start_time
        ) * 1000


# ============================================================
# Current Trace Context
# ============================================================

current_trace: ContextVar[Optional[Trace]] = ContextVar(
    "current_trace",
    default=None,
)

progress_emitter: ContextVar[Optional[Callable[[Dict[str, Any]], None]]] = ContextVar(
    "progress_emitter",
    default=None,
)


def set_current_trace(trace: Trace):
    """
    把当前请求的 Trace 放入 ContextVar。

    返回的 token 用于之后恢复原来的 Context。
    """

    return current_trace.set(trace)


def get_current_trace() -> Optional[Trace]:
    """
    获取当前异步执行上下文中的 Trace。

    instrumentation.py 的 traced_node()
    会通过这个函数取得当前请求对应的 Trace。
    """

    return current_trace.get()


def set_progress_emitter(emitter: Callable[[Dict[str, Any]], None]):
    """Attach the request-owned emitter used by the SSE adapter."""

    return progress_emitter.set(emitter)


def reset_progress_emitter(token) -> None:
    progress_emitter.reset(token)


def emit_progress(
    stage: str,
    status: str,
    message: str,
    **details: Any,
) -> None:
    """Publish a real backend stage transition when an SSE emitter exists."""

    emitter = progress_emitter.get()
    if emitter is None:
        return
    emitter(
        {
            "stage": stage,
            "status": status,
            "message": message,
            **details,
        }
    )


def reset_current_trace(token) -> None:
    """
    使用 set() 返回的 token，
    恢复 ContextVar 修改之前的状态。
    """

    current_trace.reset(token)
