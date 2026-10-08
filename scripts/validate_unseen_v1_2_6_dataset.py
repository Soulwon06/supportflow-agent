"""Validate V1.2.6 labels against the frozen KB before any model calls."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parent.parent
DATASET = ROOT / "data" / "evaluation_unseen_v1_2_6.json"
KB = ROOT / "data" / "knowledge_base.json"
OLD_EVAL = ROOT / "data" / "evaluation.json"
OLD_STRESS = ROOT / "data" / "evaluation_stress_v1_2_4.json"


def main() -> None:
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    kb = json.loads(KB.read_text(encoding="utf-8"))
    kb_by_id = {item["id"]: item for item in kb}
    old_queries = {item["query"] for item in json.loads(OLD_EVAL.read_text(encoding="utf-8"))}
    old_stress = json.loads(OLD_STRESS.read_text(encoding="utf-8"))
    old_queries.update(item["query"] for item in old_stress["cases"])

    cases = dataset["cases"]
    assert len(cases) == 30
    assert sum(item["expected_status"] == "SUPPORTED" for item in cases) == 10
    assert sum(item["expected_status"] == "PARTIAL" for item in cases) == 10
    assert sum(item["expected_status"] == "UNSUPPORTED" for item in cases) == 10
    assert len({item["id"] for item in cases}) == 30
    assert not ({item["query"] for item in cases} & old_queries)

    for case in cases:
        status = case["expected_status"]
        validation = case["validation"]
        assert case["query"].strip()
        assert case["rationale"].strip()
        if status == "SUPPORTED":
            evidence_id = case["ground_truth_evidence_id"]
            assert evidence_id in kb_by_id
            assert validation["supported_facts"]
            assert validation["missing_facts"] == []
        elif status == "PARTIAL":
            evidence_id = case["ground_truth_evidence_id"]
            assert evidence_id in kb_by_id
            assert validation["supported_facts"]
            assert validation["missing_facts"]
        else:
            assert case["ground_truth_evidence_id"] is None
            assert validation["closest_evidence_id"] in kb_by_id
            assert validation["topical_relevance"]
            assert validation["missing_facts"]

    print("DATASET_VALIDATION=PASS")
    print("CASES=30 SUPPORTED=10 PARTIAL=10 UNSUPPORTED=10")
    print("EXACT_DUPLICATES_WITH_V1_2_5_ORIGINAL_DATA=0")
    print("KB_REFERENCES_VALID=30")
    print("FIRST_MODEL_CALL_MADE=NO")


if __name__ == "__main__":
    main()
