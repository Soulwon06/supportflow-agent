"""Evaluate frozen V1.2.2 reranking on a separate hard-negative dataset."""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL"] = "true"

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.retriever import (  # noqa: E402
    dense_status,
    hybrid_candidates,
    load_real_reranker,
    real_reranker_status,
    real_rerank,
)


DATASET_PATH = PROJECT_ROOT / "data" / "evaluation_stress_v1_2_4.json"
RESULT_PATH = PROJECT_ROOT / "data" / "evaluation_results_v1_2_4_hard_negative.json"
MODEL_PATH = Path(
    os.getenv("SUPPORTFLOW_RERANKER_PATH", "/models/bge-reranker-v2-m3")
)
CANDIDATE_K = 5


def _round(value: float) -> float:
    return round(value, 8)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _stats(values: list[float]) -> dict:
    return {
        "count": len(values),
        "min": _round(min(values)),
        "max": _round(max(values)),
        "mean": _round(statistics.mean(values)),
        "median": _round(statistics.median(values)),
    }


def _rank(results: list[dict], ground_truth_id: str | None) -> int | None:
    if ground_truth_id is None:
        return None
    for rank, result in enumerate(results, 1):
        if result["id"] == ground_truth_id:
            return rank
    return None


def _validate_dataset(dataset: dict, kb: list[dict]) -> dict:
    kb_by_id = {item["id"]: item for item in kb}
    cases = dataset["cases"]
    assert len(cases) == 20
    assert sum(item["expected_answerability"] == "supported" for item in cases) == 10
    assert sum(item["expected_answerability"] == "unsupported" for item in cases) == 10
    validated = []
    for case in cases:
        assert case["id"] and case["query"] and case["rationale"]
        if case["expected_answerability"] == "supported":
            assert case["ground_truth_id"] in kb_by_id
            assert case["validation"]["ground_truth_document"] == case["ground_truth_id"]
            validated.append({"id": case["id"], "status": "validated_supported"})
        else:
            assert case["ground_truth_id"] is None
            closest = case["validation"]["closest_kb_document"]
            assert closest in kb_by_id
            assert case["validation"]["missing_fact"]
            validated.append({"id": case["id"], "status": "validated_hard_negative"})
    return {
        "status": "PASS",
        "case_count": len(cases),
        "validated_case_count": len(validated),
        "cases": validated,
    }


def main() -> None:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    kb = json.loads((PROJECT_ROOT / "data" / "knowledge_base.json").read_text(encoding="utf-8"))
    validation = _validate_dataset(dataset, kb)

    dense = dense_status()
    if not dense["available"]:
        raise RuntimeError(f"Frozen Dense model unavailable: {dense}")
    load_real_reranker(MODEL_PATH, device="cpu")
    reranker = real_reranker_status()

    scored = []
    started = time.perf_counter()
    for case in dataset["cases"]:
        candidates = hybrid_candidates(case["query"], candidate_k=CANDIDATE_K)
        results = real_rerank(case["query"], candidates, top_k=CANDIDATE_K)
        top1 = results[0] if results else {}
        ground_truth_id = case["ground_truth_id"]
        scored.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected_answerability": case["expected_answerability"],
                "ground_truth_id": ground_truth_id,
                "top1_document_id": top1.get("id"),
                "top1_rerank_score": top1.get("rerank_score"),
                "ground_truth_rank": _rank(results, ground_truth_id),
                "candidate_ids": [item["id"] for item in candidates],
                "rationale": case["rationale"],
                "validation": case["validation"],
            }
        )
    runtime = time.perf_counter() - started

    boundaries = [item for item in scored if item["expected_answerability"] == "supported"]
    hard_negatives = [item for item in scored if item["expected_answerability"] == "unsupported"]
    boundary_scores = [float(item["top1_rerank_score"]) for item in boundaries]
    hard_scores = [float(item["top1_rerank_score"]) for item in hard_negatives]

    recall = {}
    retrieval_errors = []
    for k in (1, 3, 5):
        hits = [item["ground_truth_rank"] is not None and item["ground_truth_rank"] <= k for item in boundaries]
        recall[f"recall@{k}"] = _round(sum(hits) / len(hits))
        if k == 5:
            retrieval_errors = [item["id"] for item in boundaries if item["ground_truth_rank"] is None]

    boundary_min = min(boundary_scores)
    hard_max = max(hard_scores)
    gap = boundary_min - hard_max
    if gap > 0:
        overlap_result = "CLEAR_SEPARATION"
    elif statistics.median(boundary_scores) > statistics.median(hard_scores):
        overlap_result = "PARTIAL_OVERLAP"
    else:
        overlap_result = "STRONG_OVERLAP"

    highest_hard = sorted(hard_negatives, key=lambda item: item["top1_rerank_score"], reverse=True)
    lowest_boundary = sorted(boundaries, key=lambda item: item["top1_rerank_score"])
    result = {
        "experiment_result_frozen": True,
        "dataset": str(DATASET_PATH.relative_to(PROJECT_ROOT)),
        "model_path": str(MODEL_PATH),
        "candidate_k": CANDIDATE_K,
        "dataset_validation": validation,
        "pipeline": "BM25 + Dense -> RRF -> Candidate K=5 -> local BGE reranker",
        "boundary_supported_stats": _stats(boundary_scores),
        "hard_negative_stats": _stats(hard_scores),
        "boundary_supported_min": _round(boundary_min),
        "hard_negative_max": _round(hard_max),
        "stress_separation_gap": _round(gap),
        "distribution_overlap_result": overlap_result,
        "supported_recall_at_1_3_5": recall,
        "retrieval_errors": retrieval_errors,
        "highest_scoring_hard_negatives": highest_hard,
        "lowest_scoring_supported_boundaries": lowest_boundary,
        "all_scored_cases": scored,
        "dense_status": dense,
        "reranker_status": reranker,
        "evaluation_runtime_seconds": round(runtime, 4),
        "no_threshold_selected": True,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
