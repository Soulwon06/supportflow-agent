"""Run the explicit three-query V1.2.8 real DeepSeek smoke test."""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL"] = "true"

from app.config import get_settings  # noqa: E402
from app.graph import app  # noqa: E402
from app.llm_client import build_provider  # noqa: E402
from app.nodes import configure_llm_provider  # noqa: E402
from app.observability import Trace, reset_current_trace, set_current_trace  # noqa: E402
from app.state import create_initial_state  # noqa: E402


RESULT_PATH = PROJECT_ROOT / "data" / "runtime_smoke_v1_2_8.json"

QUERIES = [
    {
        "case": "clearly_supported",
        "query": "支付失败后，重新付款前应检查银行卡余额、支付限额和网络状态吗？",
    },
    {
        "case": "clearly_partial",
        "query": "同一订单拆成多个包裹，分别查询物流时能知道它们是否同一天送达吗？",
    },
    {
        "case": "clearly_unsupported",
        "query": "银行卡已经扣款但支付失败，知识库是否说明自动解冻时间？",
    },
]


def main() -> None:
    settings = get_settings()
    if not settings.enabled:
        raise RuntimeError("DeepSeek provider is not configured")
    provider = build_provider(settings)
    if not hasattr(provider, "client"):
        raise RuntimeError("Configured provider is not the OpenAI-compatible HTTP provider")

    operation_counts = {"intent_classification": 0, "answerability_judge": 0, "grounded_generation": 0}
    original_complete = provider.complete_json

    def counted_complete(*, operation, system_prompt, user_prompt):
        operation_counts[operation] = operation_counts.get(operation, 0) + 1
        return original_complete(
            operation=operation,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )

    provider.complete_json = counted_complete
    configure_llm_provider(provider)
    records = []

    for item in QUERIES:
        trace = Trace(metadata={"thread_id": f"v1-2-8-smoke-{uuid.uuid4().hex}", "smoke_case": item["case"]})
        token = set_current_trace(trace)
        started = time.perf_counter()
        try:
            state = app.invoke(
                create_initial_state(item["query"]),
                config={"configurable": {"thread_id": trace.metadata["thread_id"]}},
            )
            trace.finish(success=not bool(state.get("error_log")))
            records.append(
                {
                    "case": item["case"],
                    "query": item["query"],
                    "intent": state.get("intent"),
                    "routing_source": state.get("routing_source"),
                    "answerability_status": state.get("answerability_status"),
                    "rag_source": state.get("rag_source"),
                    "generation_skipped": state.get("generation_skipped"),
                    "result": state.get("result"),
                    "safe_fallback_reason": state.get("safe_fallback_reason"),
                    "trace_id": trace.trace_id,
                    "spans": [
                        {
                            "name": span.name,
                            "operation": span.metadata.get("operation"),
                            "latency_ms": round(span.latency_ms or 0, 2),
                            "success": span.success,
                            "usage": span.metadata.get("usage"),
                        }
                        for span in trace.spans
                    ],
                    "llm_usage": trace.llm_usage,
                    "total_latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        except Exception as exc:
            trace.finish(success=False, error="INTERNAL_ERROR")
            records.append(
                {
                    "case": item["case"],
                    "query": item["query"],
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "trace_id": trace.trace_id,
                    "total_latency_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
        finally:
            reset_current_trace(token)

    result = {
        "phase": "SupportFlow V1.2.8",
        "offline_hf": True,
        "query_count": len(QUERIES),
        "operation_call_counts": operation_counts,
        "records": records,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
