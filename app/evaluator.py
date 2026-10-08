"""Offline retrieval evaluation for the frozen V1.2 baseline."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from .retriever import (
    bm25_search,
    dense_search,
    dense_status,
    hybrid_search,
    keyword_search,
)


DATA_DIR = Path(__file__).parent.parent / "data"
EVALUATION_PATH = DATA_DIR / "evaluation.json"
RESULT_PATH = Path(
    os.getenv(
        "SUPPORTFLOW_EVALUATION_RESULT_PATH",
        str(DATA_DIR / "evaluation_results.json"),
    )
)
RESULT_LABEL = os.getenv("SUPPORTFLOW_EVALUATION_LABEL", "v1.2-initial")
TOP_KS = (1, 3, 5)


def load_evaluation_dataset() -> list[dict]:
    with open(EVALUATION_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def calculate_recall(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    if not relevant_ids:
        return 0.0
    return len(set(retrieved_ids) & set(relevant_ids)) / len(set(relevant_ids))


def calculate_precision(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    if not retrieved_ids:
        return 0.0
    return len(set(retrieved_ids) & set(relevant_ids)) / len(retrieved_ids)


def calculate_rr(retrieved_ids: list[str], relevant_ids: list[str]) -> float:
    relevant = set(relevant_ids)
    for rank, document_id in enumerate(retrieved_ids, start=1):
        if document_id in relevant:
            return 1 / rank
    return 0.0


def _round(value: float) -> float:
    return round(value, 4)


def _metrics(cases: list[dict], top_k: int) -> dict:
    if not cases:
        return {
            f"recall@{top_k}": 0.0,
            f"precision@{top_k}": 0.0,
            "mrr": 0.0,
            "average_latency_seconds": 0.0,
        }
    return {
        f"recall@{top_k}": _round(
            sum(case[f"recall@{top_k}"] for case in cases) / len(cases)
        ),
        f"precision@{top_k}": _round(
            sum(case[f"precision@{top_k}"] for case in cases) / len(cases)
        ),
        "mrr": _round(sum(case["rr"] for case in cases) / len(cases)),
        "average_latency_seconds": _round(
            sum(case["latency_seconds"] for case in cases) / len(cases)
        ),
    }


def _failure_type(case: dict, retrieved_ids: list[str]) -> str | None:
    relevant_ids = case["relevant_ids"]
    if not relevant_ids:
        return "NO_ANSWER_FALSE_POSITIVE" if retrieved_ids else None
    ranks = [
        rank
        for rank, document_id in enumerate(retrieved_ids, start=1)
        if document_id in relevant_ids
    ]
    if not ranks:
        return "RETRIEVAL_ERROR"
    if min(ranks) > 1:
        return "RERANKING_ERROR"
    return None


def _failure_examples(cases: list[dict]) -> list[dict]:
    failed = [case for case in cases if case.get("failure_type")]
    return [
        {
            "id": case["id"],
            "query": case["query"],
            "ground_truth": case["relevant_ids"],
            "retrieved_ids": case["retrieved_ids"],
            "failure_type": case["failure_type"],
        }
        for case in failed[:5]
    ]


def evaluate_retriever(
    name: str,
    search_function: Callable,
    dataset: list[dict],
    top_ks: tuple[int, ...] = TOP_KS,
) -> dict:
    max_k = max(top_ks)
    case_results = []

    for case in dataset:
        start = time.perf_counter()
        results = search_function(case["query"], top_k=max_k)
        latency = time.perf_counter() - start
        retrieved_ids = [result["id"] for result in results]
        case_result = {
            "id": case["id"],
            "query": case["query"],
            "category": case["category"],
            "answerable": case.get("answerable", bool(case["relevant_ids"])),
            "relevant_ids": case["relevant_ids"],
            "retrieved_ids": retrieved_ids,
            "retrieved_evidence": [
                {
                    "id": result["id"],
                    "rank": result.get("rank"),
                    "source_version": result.get("source_version"),
                    "retrieval_method": result.get("retrieval_method"),
                }
                for result in results
            ],
            "latency_seconds": round(latency, 6),
            "failure_type": _failure_type(case, retrieved_ids),
        }
        for top_k in top_ks:
            ids_at_k = retrieved_ids[:top_k]
            case_result[f"recall@{top_k}"] = _round(
                calculate_recall(ids_at_k, case["relevant_ids"])
            )
            case_result[f"precision@{top_k}"] = _round(
                calculate_precision(ids_at_k, case["relevant_ids"])
            )
            case_result[f"rr@{top_k}"] = _round(
                calculate_rr(ids_at_k, case["relevant_ids"])
            )
        case_result["rr"] = case_result[f"rr@{max_k}"]
        case_results.append(case_result)

    supported = [case for case in case_results if case["answerable"]]
    unsupported = [case for case in case_results if not case["answerable"]]
    metrics_by_k = {
        str(top_k): _metrics(case_results, top_k)
        for top_k in top_ks
    }
    supported_metrics_by_k = {
        str(top_k): _metrics(supported, top_k)
        for top_k in top_ks
    }
    false_positive_count = sum(
        case["failure_type"] == "NO_ANSWER_FALSE_POSITIVE"
        for case in unsupported
    )
    failures = Counter(
        case["failure_type"]
        for case in case_results
        if case["failure_type"]
    )
    return {
        "name": name,
        "status": "enabled",
        "metrics_by_k": metrics_by_k,
        "supported_metrics_by_k": supported_metrics_by_k,
        "no_answer_metrics": {
            "case_count": len(unsupported),
            "false_positive_count": false_positive_count,
            "false_positive_rate": _round(
                false_positive_count / len(unsupported)
            )
            if unsupported
            else 0.0,
        },
        "failure_breakdown": dict(failures),
        "top_failed_cases": _failure_examples(case_results),
        "cases": case_results,
    }


def evaluate_dense(dataset: list[dict]) -> dict:
    status = dense_status()
    if not status["available"]:
        return {
            "name": "Dense",
            "status": "disabled" if not status["enabled"] else "unavailable",
            "dense_status": status,
            "metrics_by_k": None,
            "supported_metrics_by_k": None,
            "failure_breakdown": None,
            "top_failed_cases": [],
            "cases": [],
        }
    result = evaluate_retriever("Dense", dense_search, dataset)
    result["dense_status"] = status
    return result


def save_results(dataset: list[dict], retrievers: list[dict]) -> None:
    output = {
        "baseline_version": RESULT_LABEL,
        "baseline_frozen": True,
        "knowledge_base_file": "data/knowledge_base.json",
        "evaluation_file": "data/evaluation.json",
        "dataset_size": len(dataset),
        "supported_query_count": sum(case.get("answerable", bool(case["relevant_ids"])) for case in dataset),
        "no_answer_query_count": sum(not case.get("answerable", bool(case["relevant_ids"])) for case in dataset),
        "top_k_values": list(TOP_KS),
        "retrievers": retrievers,
    }
    with open(RESULT_PATH, "w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)


def print_summary(retrievers: list[dict]) -> None:
    print("=== V1.2 Retrieval Baseline ===")
    for result in retrievers:
        print(f"\n--- {result['name']} [{result['status']}] ---")
        if result["metrics_by_k"] is None:
            print(result.get("dense_status"))
            continue
        for top_k, metrics in result["metrics_by_k"].items():
            print(
                f"K={top_k}: recall={metrics[f'recall@{top_k}']} "
                f"precision={metrics[f'precision@{top_k}']} "
                f"mrr={metrics['mrr']}"
            )
        print(f"failures={result['failure_breakdown']}")
        print(f"no_answer={result['no_answer_metrics']}")
    print(f"\nSaved baseline report to {RESULT_PATH}")


def main() -> None:
    dataset = load_evaluation_dataset()
    retrievers = [
        evaluate_retriever("Char Keyword", keyword_search, dataset),
        evaluate_retriever("BM25", bm25_search, dataset),
        evaluate_dense(dataset),
        evaluate_retriever("Hybrid / RRF", hybrid_search, dataset),
    ]
    save_results(dataset, retrievers)
    print_summary(retrievers)


if __name__ == "__main__":
    main()
