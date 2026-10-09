"""Keep the entire pytest suite provider-isolated, even when .env has a real key."""

import json
import re

import pytest

from app.llm_client import LLMResponse
from app import nodes as nodes_module
from app.nodes import configure_llm_provider


class SuiteFakeProvider:
    provider_name = "pytest-fake"
    model = "pytest-fake-model"

    def complete_json(self, *, operation, system_prompt, user_prompt):
        if operation == "intent_classification":
            if re.search(r"退款政策|退款期限|退货政策|退货条件|质量问题|商品质量", user_prompt):
                intent = "knowledge"
            elif re.search(r"退款\s*\d|退\s*\d|全部退款|全额退款|退款.*订单|申请退款|我要退款|退款", user_prompt):
                intent = "refund"
            elif re.search(r"库存|现有多少|可用数量|还有多少", user_prompt):
                intent = "inventory"
            elif re.search(r"已完成|生产进度|合格数量|生产任务|完成了多少", user_prompt):
                intent = "production"
            elif re.search(r"上单|下单|创建订单|提交订单", user_prompt):
                intent = "order_entry"
            elif "物流" in user_prompt or "现在到哪" in user_prompt or "到哪里了" in user_prompt:
                intent = "logistics"
            elif "订单" in user_prompt or re.search(r"SF\d{4,}", user_prompt):
                intent = "order"
            else:
                intent = "unknown"
            content = json.dumps({"intent": intent}, ensure_ascii=False)
        elif operation == "answerability_judge":
            content = json.dumps(
                {
                    "status": "SUPPORTED",
                    "supported_facts": ["pytest evidence supports the request"],
                    "missing_facts": [],
                    "reason": "pytest fake evidence is sufficient",
                },
                ensure_ascii=False,
            )
        else:
            content = json.dumps(
                {
                    "answer": "pytest fake grounded answer",
                    "source_ids": ["quality_policy_01"],
                },
                ensure_ascii=False,
            )
        return LLMResponse(
            content=content,
            provider=self.provider_name,
            model=self.model,
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


@pytest.fixture(autouse=True)
def isolate_provider_from_real_credentials(monkeypatch):
    def fake_reranked_retrieval(query):
        return [
            {
                "id": "quality_policy_01",
                "text": "商品存在质量问题时，用户可以提交商品照片、问题描述和订单信息申请售后处理。",
                "content": "商品存在质量问题时，用户可以提交商品照片、问题描述和订单信息申请售后处理。",
                "title": "商品质量售后",
                "category": "售后",
                "source_version": "v1",
                "rerank_score": 0.9,
                "rerank_rank": 1,
                "retrieval_method": "bge-reranker-v2-m3",
            }
        ]

    monkeypatch.setenv("SUPPORTFLOW_TEST_MODE", "1")
    configure_llm_provider(SuiteFakeProvider())
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", fake_reranked_retrieval)
    yield
    configure_llm_provider(None)
