import pytest
import httpx

from app.config import SupportFlowSettings
from app.graph import app
from app.llm_client import LLMResponse, OpenAICompatibleProvider, ProviderError
from app.nodes import configure_llm_provider, knowledge_node, router_node
from app.state import create_initial_state


class FakeProvider:
    provider_name = "fake"
    model = "fake-model"

    def __init__(
        self,
        intent="{\"intent\": \"unknown\"}",
        judgment='{"status":"SUPPORTED","supported_facts":["质量问题可申请售后"],"missing_facts":[],"reason":"evidence is sufficient"}',
        grounded='{"answer": "Fake grounded answer", "source_ids": ["quality_policy_01"]}',
    ):
        self.intent = intent
        self.judgment = judgment
        self.grounded = grounded
        self.calls = []

    def complete_json(self, *, operation, system_prompt, user_prompt):
        self.calls.append((operation, system_prompt, user_prompt))
        if operation == "intent_classification":
            content = self.intent
        elif operation == "answerability_judge":
            content = self.judgment
        else:
            content = self.grounded
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self.model,
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        )


@pytest.fixture(autouse=True)
def reset_provider():
    configure_llm_provider(None)
    yield
    configure_llm_provider(None)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("退款政策是什么？", "knowledge"),
        ("商品有质量问题应该怎么处理？", "knowledge"),
        ("我要退款100元，订单9527", "refund"),
        ("查询订单9527", "order"),
        ("订单9527的物流到哪里了", "logistics"),
    ],
)
def test_intent_classification_contract(message, expected):
    provider = FakeProvider(intent=f'{{"intent": "{expected}"}}')
    configure_llm_provider(provider)

    # Isolate intent routing from the V1.5g refund authorization boundary.
    result = router_node(create_initial_state(message, role="manager"))

    assert result["intent"] == expected
    assert result["routing_source"] == "llm"


@pytest.mark.parametrize(
    "content",
    ["not-json", '{"intent": "not-an-intent"}', ""],
)
def test_invalid_intent_response_uses_safe_fallback(content):
    configure_llm_provider(FakeProvider(intent=content))

    result = router_node(create_initial_state("退款政策是什么？"))

    assert result["intent"] == "knowledge"
    assert result["routing_source"] == "keyword_fallback"
    assert result["routing_error_classification"] in {
        "INVALID_JSON",
        "SCHEMA_VALIDATION",
        "EMPTY_RESPONSE",
    }


def test_grounded_generation_receives_retrieved_evidence():
    provider = FakeProvider(
        intent='{"intent": "knowledge"}',
        grounded='{"answer": "请提交商品照片和问题描述。", "source_ids": ["quality_policy_01"]}',
    )
    configure_llm_provider(provider)

    result = app.invoke(
        create_initial_state("商品有质量问题应该怎么处理？"),
        config={"configurable": {"thread_id": "test-grounded-generation"}},
    )

    assert result["rag_source"] == "llm"
    assert "请提交商品照片" in result["result"]
    grounded_calls = [call for call in provider.calls if call[0] == "grounded_generation"]
    assert grounded_calls
    assert "quality_policy_01" in grounded_calls[0][2]
    assert "商品存在质量问题" in grounded_calls[0][2]


def test_grounded_generation_failure_returns_safe_fallback():
    provider = FakeProvider(
        intent='{"intent": "knowledge"}',
        grounded="not-json",
    )
    configure_llm_provider(provider)

    result = app.invoke(
        create_initial_state("商品有质量问题应该怎么处理？"),
        config={"configurable": {"thread_id": "test-grounded-fallback"}},
    )

    assert result["rag_source"] == "safe_fallback"
    assert result["rag_error_classification"] == "INVALID_JSON"
    assert result["result"]
    assert result["generation_skipped"] is False


def test_provider_error_does_not_enter_routing():
    class BrokenProvider(FakeProvider):
        def complete_json(self, *, operation, system_prompt, user_prompt):
            raise ProviderError("RATE_LIMITED", "fake rate limit", retryable=True)

    configure_llm_provider(BrokenProvider())
    result = router_node(create_initial_state("查询订单9527"))

    assert result["intent"] == "order"
    assert result["routing_source"] == "keyword_fallback"
    assert result["routing_error_classification"] == "RATE_LIMITED"


def test_openai_compatible_response_parses_usage_without_estimating_cost():
    provider = OpenAICompatibleProvider(
        SupportFlowSettings(api_key="test-only-key", max_retries=0)
    )
    response = httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": '{"intent":"knowledge"}'}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
        },
    )

    parsed = provider._parse_response(response)

    assert parsed.content == '{"intent":"knowledge"}'
    assert parsed.usage == {
        "input_tokens": 9,
        "output_tokens": 4,
        "total_tokens": 13,
    }


def test_openai_compatible_429_is_classified_and_not_infinite_retry():
    provider = OpenAICompatibleProvider(
        SupportFlowSettings(api_key="test-only-key", max_retries=0)
    )

    class FakeClient:
        def post(self, *args, **kwargs):
            return httpx.Response(429, json={"error": "rate limited"})

    provider.client = FakeClient()

    with pytest.raises(ProviderError) as error:
        provider.complete_json(
            operation="intent_classification",
            system_prompt="system",
            user_prompt="user",
        )

    assert error.value.classification == "RATE_LIMITED"
    assert error.value.retryable is True
