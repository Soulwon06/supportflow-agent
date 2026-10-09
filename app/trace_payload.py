"""Serialize runtime-owned trace/state data for the Web Workbench."""

from __future__ import annotations

from typing import Any, Mapping

from .observability import Trace


def _usage_from_span(span) -> dict[str, Any]:
    usage = span.metadata.get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def build_trace_payload(trace: Trace, state: Mapping[str, Any]) -> dict[str, Any]:
    """Expose actual runtime values without secrets or hidden reasoning."""
    stage_latency: dict[str, float | None] = {}
    judge_usage: dict[str, Any] | None = None
    generator_usage: dict[str, Any] | None = None
    serialized_spans = []

    for span in trace.spans:
        operation = span.metadata.get("operation")
        stage_name = operation or span.metadata.get("node") or span.name
        latency = round(span.latency_ms or 0, 2)
        serialized_spans.append(
            {
                "name": span.name,
                "operation": operation,
                "node": span.metadata.get("node"),
                "latency_ms": latency,
                "success": span.success,
                "error_classification": span.metadata.get("error_classification"),
                "usage": span.metadata.get("usage"),
                "cache_hit": span.metadata.get("cache_hit"),
                "initialization_seconds": span.metadata.get("initialization_seconds"),
            }
        )
        if operation == "intent_classification":
            stage_latency["intent"] = latency
        elif operation in {"answerability_judge", "grounded_generation"}:
            stage_latency[operation] = latency
            if operation == "answerability_judge":
                judge_usage = _usage_from_span(span)
            else:
                generator_usage = _usage_from_span(span)
        elif span.name in {"retrieval", "rerank"}:
            stage_latency[span.name] = latency
        elif span.name == "reranker_initialization":
            stage_latency["reranker_initialization"] = latency

    return {
        "trace_id": trace.trace_id,
        "intent": state.get("intent") or None,
        "route": trace.metadata.get("route") or state.get("intent") or None,
        "routing_source": state.get("routing_source") or None,
        "rag_corpus": state.get("rag_corpus") or None,
        "retrieved_source_ids": list(state.get("retrieved_source_ids") or []),
        "database_source_ids": list(state.get("database_source_ids") or []),
        "sop_source_ids": list(state.get("sop_source_ids") or []),
        "evidence": {
            "id": state.get("retrieved_evidence_id") or None,
            "corpus": state.get("retrieved_evidence_corpus") or None,
            "document_id": state.get("retrieved_evidence_document_id") or None,
            "chunk_id": state.get("retrieved_evidence_chunk_id"),
            "title": state.get("retrieved_evidence_title") or None,
            "category": state.get("retrieved_evidence_category") or None,
            "source_section": state.get("retrieved_evidence_source_section") or None,
            "version": state.get("retrieved_evidence_version") or None,
            "text": state.get("retrieved_evidence_text") or None,
        },
        "rerank_score": state.get("rerank_score"),
        "answerability_status": state.get("answerability_status") or None,
        "supported_facts": list(state.get("supported_facts") or []),
        "missing_facts": list(state.get("missing_facts") or []),
        "generation_skipped": bool(state.get("generation_skipped", False)),
        "generation_called": bool(state.get("generation_called", False)),
        "rag_source": state.get("rag_source") or None,
        "safe_fallback_reason": state.get("safe_fallback_reason") or None,
        "terminal_status": state.get("terminal_status") or None,
        "pending_action": state.get("pending_action") or None,
        "missing_fields": list(state.get("missing_fields") or []),
        "order_id": state.get("order_id") or state.get("current_order_id") or None,
        "order_total": state.get("order_total") or None,
        "refundable_amount": state.get("refundable_amount") or None,
        "refund_amount": state.get("refund_amount") or None,
        "refund_cancelled": bool(state.get("refund_cancelled", False)),
        "latency": {
            "total_ms": round(trace.latency_ms or 0, 2),
            "stages_ms": stage_latency,
        },
        "retrieval_profile": trace.metadata.get("retrieval_profile") or {},
        "retriever_startup_profile": trace.metadata.get(
            "retriever_startup_profile"
        ) or {},
        "usage": {
            "judge": judge_usage,
            "generator": generator_usage,
            "total": trace.llm_usage,
            "cost_usd": None,
        },
        "spans": serialized_spans,
    }
