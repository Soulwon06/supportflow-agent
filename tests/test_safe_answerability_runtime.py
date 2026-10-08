import json

from app.graph import app
from app.llm_client import LLMResponse
from app.nodes import configure_llm_provider
from app.state import create_initial_state


class SafetyFakeProvider:
    provider_name = "safety-fake"
    model = "safety-fake-model"

    def __init__(self, *, judgment, grounded='{"answer":"安全的知识库回答。","source_ids":["quality_policy_01"]}'):
        self.judgment = judgment
        self.grounded = grounded
        self.calls = []

    def complete_json(self, *, operation, system_prompt, user_prompt):
        self.calls.append((operation, system_prompt, user_prompt))
        if operation == "intent_classification":
            content = '{"intent":"knowledge"}'
        elif operation == "answerability_judge":
            content = self.judgment
        else:
            content = self.grounded
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self.model,
            usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        )


def invoke_knowledge(provider, thread_id):
    configure_llm_provider(provider)
    return app.invoke(
        create_initial_state("商品有质量问题应该怎么处理？"),
        config={"configurable": {"thread_id": thread_id}},
    )


def test_supported_calls_constrained_generator_and_records_evidence():
    provider = SafetyFakeProvider(
        judgment=json.dumps(
            {
                "status": "SUPPORTED",
                "supported_facts": ["可以提交照片、问题描述和订单信息"],
                "missing_facts": [],
                "reason": "evidence directly answers the request",
            },
            ensure_ascii=False,
        )
    )

    result = invoke_knowledge(provider, "safe-supported")

    operations = [call[0] for call in provider.calls]
    assert operations == ["intent_classification", "answerability_judge", "grounded_generation"]
    assert result["answerability_status"] == "SUPPORTED"
    assert result["generation_skipped"] is False
    assert result["retrieved_evidence_id"] == "quality_policy_01"
    assert result["rag_source"] == "llm"
    assert result["result"]
    assert "MISSING_FACTS" in provider.calls[2][2]


def test_partial_calls_generator_and_prompt_requires_missing_fact_disclosure():
    provider = SafetyFakeProvider(
        judgment=json.dumps(
            {
                "status": "PARTIAL",
                "supported_facts": ["可以申请售后处理"],
                "missing_facts": ["是否免费上门取件"],
                "reason": "pickup and fee details are absent",
            },
            ensure_ascii=False,
        ),
        grounded='{"answer":"可以申请售后处理；当前资料没有说明是否免费上门取件。","source_ids":["quality_policy_01"]}',
    )

    result = invoke_knowledge(provider, "safe-partial")

    assert result["answerability_status"] == "PARTIAL"
    assert result["generation_skipped"] is False
    assert "没有说明" in result["result"]
    assert "是否免费上门取件" in provider.calls[2][2]


def test_unsupported_skips_generator_and_abstains_deterministically():
    provider = SafetyFakeProvider(
        judgment=json.dumps(
            {
                "status": "UNSUPPORTED",
                "supported_facts": [],
                "missing_facts": ["上门取件和费用"],
                "reason": "the requested details are absent",
            },
            ensure_ascii=False,
        )
    )

    result = invoke_knowledge(provider, "safe-unsupported")

    assert [call[0] for call in provider.calls] == [
        "intent_classification",
        "answerability_judge",
    ]
    assert result["answerability_status"] == "UNSUPPORTED"
    assert result["generation_skipped"] is True
    assert result["rag_source"] == "answerability_abstention"
    assert "知识库没有足够信息" in result["result"]


def test_judge_validation_failure_uses_safe_fallback_without_generation():
    provider = SafetyFakeProvider(judgment="not-json")

    result = invoke_knowledge(provider, "safe-judge-failure")

    assert [call[0] for call in provider.calls] == [
        "intent_classification",
        "answerability_judge",
    ]
    assert result["rag_source"] == "safe_fallback"
    assert result["safe_fallback_reason"] == "INVALID_JSON"
    assert result["generation_skipped"] is True
    assert result["result"]


def test_generator_validation_failure_uses_safe_fallback_not_raw_evidence():
    provider = SafetyFakeProvider(
        judgment='{"status":"SUPPORTED","supported_facts":["可申请售后"],"missing_facts":[],"reason":"enough"}',
        grounded="not-json",
    )

    result = invoke_knowledge(provider, "safe-generator-failure")

    assert [call[0] for call in provider.calls] == [
        "intent_classification",
        "answerability_judge",
        "grounded_generation",
    ]
    assert result["rag_source"] == "safe_fallback"
    assert result["safe_fallback_reason"] == "INVALID_JSON"
    assert result["generation_skipped"] is False
    assert "未生成未经验证的回答" in result["result"]


def test_reranker_unavailable_uses_safe_fallback_without_network(monkeypatch):
    from app import nodes

    def unavailable(_query):
        raise FileNotFoundError("local reranker unavailable")

    monkeypatch.setattr(nodes, "_retrieve_reranked", unavailable)
    provider = SafetyFakeProvider(
        judgment='{"status":"SUPPORTED","supported_facts":[],"missing_facts":[],"reason":"unused"}'
    )

    result = invoke_knowledge(provider, "safe-reranker-failure")

    assert [call[0] for call in provider.calls] == ["intent_classification"]
    assert result["rag_source"] == "safe_fallback"
    assert result["safe_fallback_reason"] == "RETRIEVAL_OR_RERANKER_ERROR"
    assert result["generation_skipped"] is True
