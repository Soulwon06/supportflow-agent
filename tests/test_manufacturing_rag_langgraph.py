import json

from fastapi.testclient import TestClient

from app import nodes as nodes_module
from app.api import api
from app.graph import app
from app.llm_client import LLMResponse
from app.nodes import configure_llm_provider, router_node
from app.state import create_initial_state
from app.rag_models import CorpusName


class ManufacturingFakeProvider:
    provider_name = "pytest-manufacturing-fake"
    model = "pytest-manufacturing-fake-model"

    def __init__(self, *, source_ids=None, judgment="SUPPORTED"):
        self.calls = []
        self.source_ids = source_ids or ["mfg_demo_production_sop_v1"]
        self.judgment = judgment

    def complete_json(self, *, operation, system_prompt, user_prompt):
        self.calls.append((operation, system_prompt, user_prompt))
        if operation == "intent_classification":
            content = {"intent": "unknown"}
        elif operation == "answerability_judge":
            content = {
                "status": self.judgment,
                "supported_facts": ["演示证据包含制造流程说明"],
                "missing_facts": [],
                "reason": "固定测试证据足够",
            }
        else:
            content = {
                "answer": "制造演示规范回答",
                "source_ids": self.source_ids,
            }
        return LLMResponse(
            content=json.dumps(content, ensure_ascii=False),
            provider=self.provider_name,
            model=self.model,
            usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        )


def manufacturing_evidence(query, *, corpus=CorpusName.manufacturing_demo, role="production_employee"):
    assert corpus is CorpusName.manufacturing_demo
    assert role
    return [
        {
            "id": "mfg_demo_production_sop_v1",
            "document_id": "mfg_demo_production_sop_v1",
            "chunk_id": None,
            "corpus": "manufacturing_demo",
            "source_version": "demo-1.0",
            "source_section": "production_precheck",
            "title": "生产前设备、物料与工单核对演示规范",
            "category": "production_precheck",
            "content": "生产开始前应核对设备、物料、工单和版本信息。",
            "text": "生产开始前应核对设备、物料、工单和版本信息。",
            "synthetic": True,
            "authority": "teaching_demo_only",
            "allowed_roles": ["production_employee"],
            "effective_status": "active",
            "rerank_score": 0.8,
            "rank": 1,
            "retrieval_method": "bge-reranker-v2-m3",
        }
    ]


def test_manufacturing_stable_questions_route_to_manufacturing_knowledge():
    configure_llm_provider(ManufacturingFakeProvider())
    cases = [
        "生产前需要检查什么？",
        "不合格品如何隔离和记录？",
        "卷轴包装前需要核对什么？",
        "出库作业规范是什么？",
        "员工申请出库需要经过什么流程？",
    ]

    for message in cases:
        result = router_node(create_initial_state(message, role="employee"))
        assert result["intent"] == "manufacturing_knowledge"
        assert result["rag_corpus"] == "manufacturing_demo"


def test_manufacturing_graph_reuses_grounded_knowledge_chain(monkeypatch):
    provider = ManufacturingFakeProvider()
    configure_llm_provider(provider)
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", manufacturing_evidence)

    result = app.invoke(
        create_initial_state("生产前需要检查什么？", role="production_employee"),
        config={"configurable": {"thread_id": "b4-manufacturing-chain"}},
    )

    assert result["intent"] == "manufacturing_knowledge"
    assert result["rag_corpus"] == "manufacturing_demo"
    assert result["retrieved_source_ids"] == ["mfg_demo_production_sop_v1"]
    assert result["retrieved_evidence_corpus"] == "manufacturing_demo"
    assert result["answerability_status"] == "SUPPORTED"
    assert result["generation_called"] is True
    assert result["rag_source"] == "llm"
    assert "制造演示规范回答" in result["result"]
    assert [call[0] for call in provider.calls] == [
        "intent_classification",
        "answerability_judge",
        "grounded_generation",
    ]


def test_database_and_write_intents_keep_their_existing_routes():
    configure_llm_provider(ManufacturingFakeProvider())

    order_result = router_node(
        create_initial_state("订单 SF1001 现在完成多少？", role="employee")
    )
    outbound_result = router_node(
        create_initial_state("帮我提交出库申请。", role="warehouse_employee")
    )
    customer_result = router_node(
        create_initial_state("客服退款政策是什么？", role="employee")
    )

    assert order_result["intent"] == "order"
    assert outbound_result["intent"] == "outbound_submit"
    assert customer_result["intent"] == "knowledge"
    assert customer_result["rag_corpus"] == "customer_support"


def test_forged_grounded_source_id_falls_back_safely(monkeypatch):
    provider = ManufacturingFakeProvider(source_ids=["forged-source-id"])
    configure_llm_provider(provider)
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", manufacturing_evidence)

    result = app.invoke(
        create_initial_state("生产前需要检查什么？", role="production_employee"),
        config={"configurable": {"thread_id": "b4-forged-source"}},
    )

    assert result["rag_source"] == "safe_fallback"
    assert result["safe_fallback_reason"] == "SCHEMA_VALIDATION"
    assert result["generation_called"] is True
    assert result["terminal_status"] == "SAFE_FALLBACK"


def test_unsupported_manufacturing_evidence_skips_generation(monkeypatch):
    provider = ManufacturingFakeProvider(judgment="UNSUPPORTED")
    configure_llm_provider(provider)
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", manufacturing_evidence)

    result = app.invoke(
        create_initial_state("生产前需要检查什么？", role="production_employee"),
        config={"configurable": {"thread_id": "b4-unsupported"}},
    )

    assert result["answerability_status"] == "UNSUPPORTED"
    assert result["generation_called"] is False
    assert result["generation_skipped"] is True
    assert not any(call[0] == "grounded_generation" for call in provider.calls)


def test_chat_and_stream_expose_manufacturing_route_and_corpus(monkeypatch):
    provider = ManufacturingFakeProvider()
    configure_llm_provider(provider)
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", manufacturing_evidence)
    client = TestClient(api)

    response = client.post(
        "/chat",
        json={
            "message": "生产前需要检查什么？",
            "thread_id": "b4-chat",
            "role": "production_employee",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "manufacturing_knowledge"
    assert body["trace"]["rag_corpus"] == "manufacturing_demo"
    assert body["trace"]["retrieved_source_ids"] == [
        "mfg_demo_production_sop_v1"
    ]

    stream = client.post(
        "/chat/stream",
        json={
            "message": "生产前需要检查什么？",
            "thread_id": "b4-stream",
            "role": "production_employee",
        },
    )
    assert stream.status_code == 200
    assert "event: route" in stream.text
    assert '"rag_corpus": "manufacturing_demo"' in stream.text
    assert '"source_ids": ["mfg_demo_production_sop_v1"]' in stream.text
