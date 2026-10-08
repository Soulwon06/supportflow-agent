"""Offline evaluation of the existing DeepSeek provider as an evidence judge."""

from __future__ import annotations

import json
import math
import statistics
import sys
import time
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.llm_client import ProviderError, build_provider  # noqa: E402
from app.llm_service import _parse_json_object  # noqa: E402


STRESS_RESULT_PATH = PROJECT_ROOT / "data" / "evaluation_results_v1_2_4_hard_negative.json"
KB_PATH = PROJECT_ROOT / "data" / "knowledge_base.json"
RESULT_PATH = PROJECT_ROOT / "data" / "evaluation_results_v1_2_5_answerability_judge.json"
RETRIEVAL_ERROR_CASES = {"boundary_007"}

JUDGE_SYSTEM_PROMPT = """You are an evidence sufficiency judge.

Decide only whether the supplied Evidence explicitly contains enough information
to answer this specific Question.

Rules:
- Use only the Question and Evidence in the user message.
- Do not use general knowledge, common sense, assumptions, likely platform behavior,
  or pretrained knowledge.
- Topical relevance is not sufficient.
- If the specific fact required by the Question is absent from Evidence, answer false.
- If answering requires adding a fact not stated or directly supported by Evidence,
  answer false.
- Return exactly one JSON object with only these fields:
  {"answerable": true or false, "reason": "brief evidence-based reason"}
"""


class AnswerabilityJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answerable: StrictBool
    reason: str = Field(min_length=1)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


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


def _metrics(records: list[dict]) -> tuple[dict, dict]:
    usable = [record for record in records if record.get("judge_answerable") is not None]
    tp = sum(record["expected_answerability"] == "supported" and record["judge_answerable"] for record in usable)
    tn = sum(record["expected_answerability"] == "unsupported" and not record["judge_answerable"] for record in usable)
    fp = sum(record["expected_answerability"] == "unsupported" and record["judge_answerable"] for record in usable)
    fn = sum(record["expected_answerability"] == "supported" and not record["judge_answerable"] for record in usable)
    total = len(usable)
    accuracy = (tp + tn) / total if total else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return (
        {
            "eligible_cases": total,
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        },
        {"TP": tp, "TN": tn, "FP": fp, "FN": fn},
    )


def _judge_prompt(question: str, evidence_text: str) -> str:
    return f"Question:\n{question}\n\nEvidence:\n{evidence_text}"


def main() -> None:
    stress = json.loads(STRESS_RESULT_PATH.read_text(encoding="utf-8"))
    kb = json.loads(KB_PATH.read_text(encoding="utf-8"))
    kb_by_id = {item["id"]: item for item in kb}
    cases = stress["all_scored_cases"]
    assert len(cases) == 20

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
    output_tokens = 0
    total_tokens = 0

    for case in cases:
        evidence_id = case["top1_document_id"]
        evidence_text = str(kb_by_id[evidence_id].get("content") or kb_by_id[evidence_id].get("text") or "")
        started = time.perf_counter()
        judgment = None
        failure = None
        usage = {}
        try:
            response = provider.complete_json(
                operation="answerability_judge",
                system_prompt=JUDGE_SYSTEM_PROMPT,
                user_prompt=_judge_prompt(case["query"], evidence_text),
            )
            usage = response.usage or {}
            judgment = AnswerabilityJudgment.model_validate(_parse_json_object(response.content))
        except (ProviderError, ValidationError) as exc:
            failure = type(exc).__name__
        latency = time.perf_counter() - started
        latencies.append(latency)
        input_tokens += int(usage.get("input_tokens") or 0)
        output_tokens += int(usage.get("output_tokens") or 0)
        total_tokens += int(usage.get("total_tokens") or 0)

        expected = case["expected_answerability"]
        predicted = None if judgment is None else bool(judgment.answerable)
        records.append(
            {
                "id": case["id"],
                "question": case["query"],
                "expected_answerability": expected,
                "top1_evidence_id": evidence_id,
                "rerank_score": case["top1_rerank_score"],
                "judge_answerable": predicted,
                "judge_reason": None if judgment is None else judgment.reason,
                "correct": None if predicted is None else (predicted == (expected == "supported")),
                "retrieval_error": case["id"] in RETRIEVAL_ERROR_CASES,
                "judge_failure": failure,
                "usage": usage,
                "latency_seconds": round(latency, 6),
            }
        )

    end_to_end_metrics, end_to_end_confusion = _metrics(records)
    judge_only_records = [record for record in records if not record["retrieval_error"]]
    judge_only_metrics, judge_only_confusion = _metrics(judge_only_records)
    end_to_end_failures = {
        "retrieval_error_cases": [record["id"] for record in records if record["retrieval_error"]],
        "judge_false_positives": [
            record["id"] for record in records
            if record["judge_answerable"] is True and record["expected_answerability"] == "unsupported"
        ],
        "judge_false_negatives_excluding_retrieval_errors": [
            record["id"] for record in judge_only_records
            if record["judge_answerable"] is False and record["expected_answerability"] == "supported"
        ],
        "retrieval_mediated_false_negatives": [
            record["id"] for record in records
            if record["retrieval_error"] and record["judge_answerable"] is False
        ],
        "provider_or_validation_failures": [record["id"] for record in records if record["judge_failure"]],
    }
    high_score_ids = {"hard_negative_006", "hard_negative_007", "hard_negative_009", "hard_negative_010"}
    high_score_results = [record for record in records if record["id"] in high_score_ids]

    result = {
        "experiment_result_frozen": True,
        "dataset": "data/evaluation_stress_v1_2_4.json",
        "retrieval_artifact": str(STRESS_RESULT_PATH.relative_to(PROJECT_ROOT)),
        "judge_input_contract": "question + actual retrieved Top-1 evidence text only",
        "judge_prompt_version": "answerability-judge-v1",
        "total_judge_calls": provider_call_count,
        "judge_only_metrics": judge_only_metrics,
        "judge_only_confusion_matrix": judge_only_confusion,
        "end_to_end_metrics": end_to_end_metrics,
        "end_to_end_confusion_matrix": end_to_end_confusion,
        "end_to_end_failures": end_to_end_failures,
        "case_by_case_results": records,
        "high_score_hard_negative_results": high_score_results,
        "judge_false_positives": end_to_end_failures["judge_false_positives"],
        "judge_false_negatives": end_to_end_failures["judge_false_negatives_excluding_retrieval_errors"],
        "token_usage": {
            "input_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "latency": _latency_stats(latencies),
        "cost": None,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
