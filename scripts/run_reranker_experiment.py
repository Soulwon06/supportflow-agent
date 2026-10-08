"""Run the single frozen V1.2.2 local reranker experiment."""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

# This experiment is strictly local/offline, including the frozen Dense model.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"
os.environ["SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL"] = "true"

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import torch

from app.evaluator import (
    calculate_precision,
    calculate_recall,
    calculate_rr,
    load_evaluation_dataset,
)
from app.retriever import (
    dense_status,
    hybrid_candidates,
    load_real_reranker,
    mock_rerank,
    real_reranker_status,
    real_rerank,
)


CANDIDATE_K = 5
TOP_KS = (1, 3, 5)
MODEL_PATH = Path(
    os.getenv("SUPPORTFLOW_RERANKER_PATH", "/models/bge-reranker-v2-m3")
)
RESULT_PATH = PROJECT_ROOT / "data" / "evaluation_results_v1_2_2_reranker.json"
KNOWN_CASE_IDS = {"eval_002", "eval_007", "eval_014", "eval_024"}


def _round(value: float) -> float:
    return round(value, 4)


def _rank(results: list[dict], relevant_ids: list[str]) -> int | None:
    relevant = set(relevant_ids)
    for rank, result in enumerate(results, 1):
        if result["id"] in relevant:
            return rank
    return None


def _score_for(results: list[dict], relevant_ids: list[str]) -> float | None:
    relevant = set(relevant_ids)
    for result in results:
        if result["id"] in relevant:
            score = result.get("rerank_score")
            return None if score is None else float(score)
    return None


def _metrics(cases: list[dict]) -> dict:
    metrics = {}
    for top_k in TOP_KS:
        recalls = []
        precisions = []
        reciprocal_ranks = []
        for case in cases:
            ids = case["retrieved_ids"][:top_k]
            recalls.append(calculate_recall(ids, case["relevant_ids"]))
            precisions.append(calculate_precision(ids, case["relevant_ids"]))
            reciprocal_ranks.append(calculate_rr(ids, case["relevant_ids"]))
        metrics[str(top_k)] = {
            f"recall@{top_k}": _round(sum(recalls) / len(recalls)),
            f"precision@{top_k}": _round(sum(precisions) / len(precisions)),
            "mrr": _round(sum(reciprocal_ranks) / len(reciprocal_ranks)),
        }
    return metrics


def _case_record(case: dict, results: list[dict], latency_seconds: float) -> dict:
    ids = [result["id"] for result in results]
    return {
        "id": case["id"],
        "query": case["query"],
        "ground_truth": case["relevant_ids"],
        "relevant_ids": case["relevant_ids"],
        "answerable": case.get("answerable", bool(case["relevant_ids"])),
        "retrieved_ids": ids,
        "latency_seconds": round(latency_seconds, 6),
        "rank": _rank(results, case["relevant_ids"]),
        "rerank_score": _score_for(results, case["relevant_ids"]),
        "retrieved_evidence": [
            {
                "id": result["id"],
                "source_id": result.get("source_id", result["id"]),
                "source_version": result.get("source_version"),
                "retrieval_method": result.get("retrieval_method"),
                "original_rank": result.get("original_rank"),
                "rerank_score": result.get("rerank_score"),
                "rerank_rank": result.get("rerank_rank"),
                "rerank_latency_ms": result.get("rerank_latency_ms"),
            }
            for result in results
        ],
    }


def _failure_type(case: dict) -> str | None:
    if not case["answerable"]:
        return "NO_ANSWER_FALSE_POSITIVE" if case["retrieved_ids"] else None
    if case["rank"] is None:
        return "RETRIEVAL_ERROR"
    if case["rank"] > 1:
        return "RERANKING_ERROR"
    return None


def main() -> None:
    dataset = load_evaluation_dataset()
    assert len(dataset) == 35
    assert sum(bool(case.get("relevant_ids")) for case in dataset) == 30
    assert sum(not bool(case.get("relevant_ids")) for case in dataset) == 5
    assert MODEL_PATH.is_dir()

    dense = dense_status()
    if not dense["available"]:
        raise RuntimeError(f"Frozen Dense model unavailable: {dense}")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    load_real_reranker(MODEL_PATH, device=device)
    reranker_status = real_reranker_status()

    before_cases = []
    after_cases = []
    rerank_latencies_ms = []
    before_runtime = 0.0
    after_runtime = 0.0
    evaluation_started = time.perf_counter()

    for case in dataset:
        candidates = hybrid_candidates(case["query"], candidate_k=CANDIDATE_K)
        if len(candidates) > CANDIDATE_K:
            raise AssertionError("Candidate K exceeded 5")

        before_started = time.perf_counter()
        before_results = mock_rerank(case["query"], candidates, top_k=CANDIDATE_K)
        before_elapsed = time.perf_counter() - before_started
        before_runtime += before_elapsed

        after_started = time.perf_counter()
        after_results = real_rerank(case["query"], candidates, top_k=CANDIDATE_K)
        after_elapsed = time.perf_counter() - after_started
        after_runtime += after_elapsed

        if {item["id"] for item in before_results} != {item["id"] for item in after_results}:
            raise AssertionError("Real reranker changed the candidate set")
        for item in after_results:
            if item["retrieval_method"] != "rrf":
                raise AssertionError("Real reranker did not preserve retrieval_method")
            if item.get("original_rank") is None:
                raise AssertionError("Real reranker did not preserve original rank")

        before_case = _case_record(case, before_results, before_elapsed)
        after_case = _case_record(case, after_results, after_elapsed)
        before_case["failure_type"] = _failure_type(before_case)
        after_case["failure_type"] = _failure_type(after_case)
        before_cases.append(before_case)
        after_cases.append(after_case)

        if after_results:
            latency = after_results[0].get("rerank_latency_ms")
            if latency is not None:
                rerank_latencies_ms.append(float(latency))

    total_runtime = time.perf_counter() - evaluation_started

    supported_after = [case for case in after_cases if case["answerable"]]
    failure_breakdown = Counter(
        case["failure_type"]
        for case in supported_after
        if case["failure_type"]
    )
    transitions = []
    for before, after in zip(before_cases, after_cases):
        if not before["answerable"]:
            continue
        old_rank = before["rank"]
        new_rank = after["rank"]
        if old_rank is not None and new_rank is not None and new_rank < old_rank:
            transition = "rescued"
        elif old_rank is not None and new_rank is not None and new_rank > old_rank:
            transition = "harmed"
        else:
            transition = "unchanged"
        transitions.append(
            {
                "id": before["id"],
                "query": before["query"],
                "rank_before": old_rank,
                "rank_after": new_rank,
                "transition": transition,
            }
        )

    rescued = [item for item in transitions if item["transition"] == "rescued"]
    harmed = [item for item in transitions if item["transition"] == "harmed"]
    unchanged = [item for item in transitions if item["transition"] == "unchanged"]
    top1_improvements = [
        item for item in transitions if item["rank_after"] == 1 and item["rank_before"] != 1
    ]
    top1_regressions = [
        item for item in transitions if item["rank_before"] == 1 and item["rank_after"] != 1
    ]

    known_cases = []
    for before, after in zip(before_cases, after_cases):
        if before["id"] not in KNOWN_CASE_IDS:
            continue
        known_cases.append(
            {
                "id": before["id"],
                "query": before["query"],
                "ground_truth": before["ground_truth"],
                "rank_before": before["rank"],
                "rank_after": after["rank"],
                "rerank_score": after["rerank_score"],
                "transition": next(
                    item["transition"] for item in transitions if item["id"] == before["id"]
                ),
            }
        )

    no_answer_scores = [
        {
            "id": case["id"],
            "query": case["query"],
            "highest_rerank_score": max(
                (item["rerank_score"] for item in case["retrieved_evidence"]),
                default=None,
            ),
        }
        for case in after_cases
        if not case["answerable"]
    ]

    model_file = MODEL_PATH / "model.safetensors"
    result = {
        "experiment_result_frozen": True,
        "candidate_k": CANDIDATE_K,
        "model": "BAAI/bge-reranker-v2-m3",
        "model_path": str(MODEL_PATH),
        "model_size_bytes": model_file.stat().st_size,
        "cpu_or_gpu": reranker_status["device"],
        "model_initialization_time_seconds": round(
            reranker_status["initialization_seconds"], 4
        ),
        "dense_status": dense,
        "before": {
            "name": "Frozen Hybrid/RRF + mock pass-through",
            "metrics": _metrics(before_cases),
            "runtime_seconds": round(before_runtime, 4),
            "cases": before_cases,
        },
        "after": {
            "name": "Frozen Hybrid/RRF + local BAAI/bge-reranker-v2-m3",
            "metrics": _metrics(after_cases),
            "runtime_seconds": round(after_runtime, 4),
            "failure_breakdown_supported": dict(failure_breakdown),
            "cases": after_cases,
        },
        "known_cases_before_after": known_cases,
        "rescued_cases": rescued,
        "harmed_cases": harmed,
        "unchanged_cases": unchanged,
        "top1_improvements": top1_improvements,
        "top1_regressions": top1_regressions,
        "failure_attribution_after_supported": {
            "RETRIEVAL_ERROR": [
                case["id"] for case in supported_after if case["failure_type"] == "RETRIEVAL_ERROR"
            ],
            "RERANKING_ERROR": [
                case["id"] for case in supported_after if case["failure_type"] == "RERANKING_ERROR"
            ],
        },
        "total_evaluation_runtime_seconds": round(total_runtime, 4),
        "per_query_rerank_latency_ms": {
            "mean": round(statistics.mean(rerank_latencies_ms), 3),
            "median": round(statistics.median(rerank_latencies_ms), 3),
            "min": round(min(rerank_latencies_ms), 3),
            "max": round(max(rerank_latencies_ms), 3),
        },
        "no_answer_score_observation": no_answer_scores,
    }
    RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
