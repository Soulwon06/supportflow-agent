"""Run the first frozen V1.2.6 three-class answerability evaluation."""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL"] = "true"

from app.config import get_settings  # noqa: E402
from app.llm_client import ProviderError, build_provider  # noqa: E402
from app.llm_service import _parse_json_object  # noqa: E402
from app.retriever import (  # noqa: E402
    dense_status,
    hybrid_candidates,
    load_real_reranker,
    real_rerank,
    real_reranker_status,
)


DATASET_PATH = PROJECT_ROOT / "data" / "evaluation_unseen_v1_2_6.json"
KB_PATH = PROJECT_ROOT / "data" / "knowledge_base.json"
RESULT_PATH = PROJECT_ROOT / "data" / "evaluation_results_v1_2_6_three_class_judge.json"
MODEL_PATH = Path(
    os.getenv("SUPPORTFLOW_RERANKER_PATH", "/models/bge-reranker-v2-m3")
)
CANDIDATE_K = 5
STATUSES = ("SUPPORTED", "PARTIAL", "UNSUPPORTED")

JUDGE_SYSTEM_PROMPT = """You are an evidence sufficiency judge for a customer-support knowledge base.

Use only QUESTION and EVIDENCE from the user message. Do not use general knowledge,
common sense, assumptions, likely platform behavior, or pretrained knowledge.
Topical relevance is not enough. Do not infer unstated capabilities, prohibitions,
policies, time limits, guarantees, or outcomes.

Classify the evidence for the specific question:
- SUPPORTED: the evidence contains enough information for the core question and key
  requested details without adding unsupported facts.
- PARTIAL: the evidence supports a useful part, but at least one material requested
  detail is absent. A safe answer could state the supported part and identify what is
  unavailable.
- UNSUPPORTED: the evidence does not contain enough information for the core question.

Return exactly one JSON object with only these fields:
{"status":"SUPPORTED|PARTIAL|UNSUPPORTED","supported_facts":["..."],"missing_facts":["..."],"reason":"brief evidence-grounded explanation"}
"""


class AnswerabilityJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["SUPPORTED", "PARTIAL", "UNSUPPORTED"]
    supported_facts: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


def _metrics(records: list[dict]) -> tuple[dict, dict, dict]:
    usable = [record for record in records if record.get("predicted_status") in STATUSES]
    matrix = {actual: {predicted: 0 for predicted in STATUSES} for actual in STATUSES}
    for record in usable:
        matrix[record["expected_status"]][record["predicted_status"]] += 1

    per_class = {}
    for label in STATUSES:
        tp = matrix[label][label]
        fp = sum(matrix[actual][label] for actual in STATUSES if actual != label)
        fn = sum(matrix[label][predicted] for predicted in STATUSES if predicted != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": sum(matrix[label].values()),
        }
    total = len(usable)
    accuracy = sum(matrix[label][label] for label in STATUSES) / total if total else 0.0
    macro_precision = statistics.mean(item["precision"] for item in per_class.values()) if per_class else 0.0
    macro_recall = statistics.mean(item["recall"] for item in per_class.values()) if per_class else 0.0
    macro_f1 = statistics.mean(item["f1"] for item in per_class.values()) if per_class else 0.0
    overall = {
        "eligible_cases": total,
        "accuracy": round(accuracy, 4),
        "macro_precision": round(macro_precision, 4),
        "macro_recall": round(macro_recall, 4),
        "macro_f1": round(macro_f1, 4),
    }
    return overall, per_class, matrix


def _latency_stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "mean_seconds": None, "median_seconds": None, "min_seconds": None, "max_seconds": None}
    return {
        "count": len(values),
        "mean_seconds": round(statistics.mean(values), 4),
        "median_seconds": round(statistics.median(values), 4),
        "min_seconds": round(min(values), 4),
        "max_seconds": round(max(values), 4),
    }


def _judge_prompt(question: str, evidence_text: str) -> str:
    return f"QUESTION:\n{question}\n\nEVIDENCE:\n{evidence_text}"


def _misclassification(record: dict) -> dict:
    expected = record["expected_status"]
    predicted = record["predicted_status"]
    return {
        "id": record["id"],
        "query": record["query"],
        "expected": expected,
        "predicted": predicted,
        "evidence_id": record["top1_evidence_id"],
        "rerank_score": record["rerank_score"],
        "judge_reason": record["reason"],
        "retrieval_mediated_failure": record["retrieval_mediated_failure"],
        "why_label_is_wrong": record["label_rationale"],
    }


def main() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    kb_by_id = {item["id"]: item for item in kb}
    cases = dataset["cases"]
    assert len(cases) == 30

    # Freeze and resolve the actual Top-1 evidence before the first paid call.
    dense = dense_status()
    if not dense["available"]:
        raise RuntimeError(f"Frozen Dense model unavailable: {dense}")
    load_real_reranker(MODEL_PATH, device="cpu")
    reranker_status = real_reranker_status()
    retrieval_records = []
    for case in cases:
        candidates = hybrid_candidates(case["query"], candidate_k=CANDIDATE_K)
        reranked = real_rerank(case["query"], candidates, top_k=CANDIDATE_K)
        top1 = reranked[0]
        expected_evidence_id = case["ground_truth_evidence_id"]
        if expected_evidence_id is None:
            expected_evidence_id = case["validation"]["closest_evidence_id"]
        retrieval_records.append(
            {
                "case": case,
                "top1_evidence_id": top1["id"],
                "rerank_score": top1["rerank_score"],
                "evidence_text": str(kb_by_id[top1["id"]].get("content") or kb_by_id[top1["id"]].get("text") or ""),
                "retrieval_mediated_failure": top1["id"] != expected_evidence_id,
            }
        )

    settings = get_settings()
    if not settings.enabled:
        raise RuntimeError("DeepSeek provider is not configured")
    provider = build_provider(settings)
    if not hasattr(provider, "client"):
        raise RuntimeError("Configured provider is not the OpenAI-compatible HTTP provider")

    provider_call_count = 0
    original_post = provider.client.post

    def counted_post(*args, **kwargs):
        nonlocal provider_call_count
        provider_call_count += 1
        return original_post(*args, **kwargs)

    provider.client.post = counted_post

    records = []
    latencies = []
    input_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    for item in retrieval_records:
        case = item["case"]
        judgment = None
        failure = None
        usage = {}
        started = time.perf_counter()
        try:
            response = provider.complete_json(
                operation="answerability_judge_v2",
                system_prompt=JUDGE_SYSTEM_PROMPT,
                user_prompt=_judge_prompt(case["query"], item["evidence_text"]),
            )
            usage = response.usage or {}
            judgment = AnswerabilityJudgment.model_validate(_parse_json_object(response.content))
        except (ProviderError, ValidationError) as exc:
            failure = type(exc).__name__
        latency = time.perf_counter() - started
        latencies.append(latency)
        input_tokens += int(usage.get("input_tokens") or 0)
        completion_tokens += int(usage.get("output_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)

        predicted = None if judgment is None else judgment.status
        case_record = {
            "id": case["id"],
            "query": case["query"],
            "expected_status": case["expected_status"],
            "predicted_status": predicted,
            "top1_evidence_id": item["top1_evidence_id"],
            "rerank_score": item["rerank_score"],
            "supported_facts": [] if judgment is None else judgment.supported_facts,
            "missing_facts": [] if judgment is None else judgment.missing_facts,
            "reason": None if judgment is None else judgment.reason,
            "correct": None if predicted is None else predicted == case["expected_status"],
            "retrieval_mediated_failure": item["retrieval_mediated_failure"],
            "judge_failure": failure,
            "label_rationale": case["rationale"],
            "usage": usage,
            "latency_seconds": round(latency, 6),
        }
        records.append(case_record)

    judge_only_records = [record for record in records if not record["retrieval_mediated_failure"]]
    judge_only_metrics, judge_only_per_class, judge_only_matrix = _metrics(judge_only_records)
    end_to_end_metrics, end_to_end_per_class, end_to_end_matrix = _metrics(records)

    all_misclassifications = [
        _misclassification(record)
        for record in records
        if record["predicted_status"] in STATUSES and not record["correct"]
    ]
    safety_categories = {}
    for category in (
        ("UNSUPPORTED", "SUPPORTED"),
        ("UNSUPPORTED", "PARTIAL"),
        ("SUPPORTED", "UNSUPPORTED"),
        ("PARTIAL", "SUPPORTED"),
        ("PARTIAL", "UNSUPPORTED"),
    ):
        key = f"{category[0]}_TO_{category[1]}"
        safety_categories[key] = [
            item for item in all_misclassifications
            if item["expected"] == category[0] and item["predicted"] == category[1]
        ]

    result = {
        "experiment_result_frozen": True,
        "dataset": str(DATASET_PATH.relative_to(PROJECT_ROOT)),
        "pipeline": "BM25 + Dense -> RRF -> Candidate K=5 -> local BGE reranker",
        "judge_contract": "SUPPORTED | PARTIAL | UNSUPPORTED",
        "judge_prompt_version": "answerability-v2-general-principles-only",
        "judge_input_contract": "QUESTION + actual Top-1 EVIDENCE text only",
        "total_judge_calls": provider_call_count,
        "judge_only_metrics": judge_only_metrics,
        "judge_only_per_class_metrics": judge_only_per_class,
        "judge_only_confusion_matrix": judge_only_matrix,
        "end_to_end_metrics": end_to_end_metrics,
        "end_to_end_per_class_metrics": end_to_end_per_class,
        "end_to_end_confusion_matrix": end_to_end_matrix,
        "retrieval_mediated_failures": [
            record["id"] for record in records if record["retrieval_mediated_failure"]
        ],
        "safety_critical_errors": safety_categories,
        "all_misclassifications": all_misclassifications,
        "token_usage": {
            "input_tokens": input_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
        "latency": _latency_stats(latencies),
        "cost": None,
        "provider_or_validation_failures": [record["id"] for record in records if record["judge_failure"]],
        "reranker_status": reranker_status,
        "case_by_case_results": records,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
