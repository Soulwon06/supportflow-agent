import json

from fastapi.testclient import TestClient

from app import nodes as nodes_module
from app.api import api
from app.database import get_quality_reports
from app.graph import app
from app.state import create_initial_state
from app.tool_models import success_result
from app.retriever import CorpusName, CorpusUnavailableError


SOP_EVIDENCE = {
    "id": "mfg_demo_quality_sop_v1",
    "document_id": "mfg_demo_quality_sop_v1",
    "chunk_id": None,
    "corpus": "manufacturing_demo",
    "source_version": "demo-1.0",
    "source_section": "quality_inspection",
    "title": "质检检查与不合格品隔离、记录、复检演示规范",
    "content": "不合格品应隔离并标识，记录缺陷与批次信息，完成复检后再决定放行、返工或报废。",
    "text": "不合格品应隔离并标识，记录缺陷与批次信息，完成复检后再决定放行、返工或报废。",
    "synthetic": True,
    "authority": "teaching_demo_only",
    "allowed_roles": ["manager"],
    "effective_status": "active",
    "rerank_score": 0.91,
    "rank": 1,
    "retrieval_method": "bge-reranker-v2-m3",
}


def _mixed_sop(*args, **kwargs):
    assert kwargs["corpus"] is CorpusName.manufacturing_demo
    return [SOP_EVIDENCE]


def _state(message, *, role="manager"):
    state = create_initial_state(message, role=role)
    state["permissions"] = ["query_quality", "query_production"]
    return state


def test_mixed_query_combines_trusted_db_fact_and_sop(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "mixed.sqlite"))
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", _mixed_sop)

    result = app.invoke(
        _state("SF1001 的质检为什么没有通过？应该怎么处理？"),
        config={"configurable": {"thread_id": "b4-2-mixed-success"}},
    )

    assert result["intent"] == "quality_mixed"
    assert result["terminal_status"] == "SUCCESS"
    assert "不合格 20 个" in result["result"]
    assert "具体失败原因未记录" in result["result"]
    assert "不合格品应隔离并标识" in result["result"]
    assert result["database_source_ids"] == ["db:quality:SF1001:2026-08-25"]
    assert result["sop_source_ids"] == ["mfg_demo_quality_sop_v1"]
    assert result["generation_called"] is False


def test_mixed_query_does_not_invent_missing_failure_reason(monkeypatch):
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", _mixed_sop)
    monkeypatch.setattr(
        nodes_module,
        "dispatch_tool",
        lambda *args, **kwargs: success_result(
            {
                "items": [
                    {
                        "order_no": "A001",
                        "sku": "REEL-DEMO",
                        "inspected_quantity": 100,
                        "qualified_quantity": 90,
                        "rejected_quantity": 10,
                        "report_date": "2026-09-01",
                        "remark": "",
                    }
                ]
            }
        ),
    )

    result = nodes_module.quality_mixed_node(
        _state("订单 A001 的质检为什么没有通过？应该怎么处理？")
    )

    assert "不合格 10 个" in result["result"]
    assert "不能据此猜测" in result["result"]
    assert "不合格品应隔离并标识" in result["result"]


def test_mixed_query_with_no_quality_record_fails_without_sop_fabrication(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "missing.sqlite"))
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", _mixed_sop)

    result = app.invoke(
        _state("SF9999 的质检为什么没有通过？应该怎么处理？"),
        config={"configurable": {"thread_id": "b4-2-missing-order"}},
    )

    assert result["terminal_status"] == "FAILED"
    assert result["error_log"].startswith("NOT_FOUND")
    assert "没有找到匹配的质检记录" in result["result"]
    assert result["sop_source_ids"] == []


def test_mixed_query_with_passed_quality_record_does_not_claim_failure(monkeypatch):
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", _mixed_sop)
    monkeypatch.setattr(
        nodes_module,
        "dispatch_tool",
        lambda *args, **kwargs: success_result(
            {
                "items": [
                    {
                        "order_no": "A002",
                        "sku": "REEL-DEMO",
                        "inspected_quantity": 100,
                        "qualified_quantity": 100,
                        "rejected_quantity": 0,
                        "report_date": "2026-09-01",
                        "remark": "",
                    }
                ]
            }
        ),
    )

    result = nodes_module.quality_mixed_node(
        _state("订单 A002 的质检为什么没有通过？应该怎么处理？")
    )

    assert "不合格数量为 0" in result["result"]
    assert "未发现质检未通过事实" in result["result"]


def test_mixed_query_handles_sop_permission_without_leaking_sop(monkeypatch):
    monkeypatch.setattr(
        nodes_module,
        "_retrieve_reranked",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            CorpusUnavailableError(
                CorpusName.manufacturing_demo,
                "NO_AUTHORIZED_ACTIVE_DOCUMENTS",
            )
        ),
    )

    result = nodes_module.quality_mixed_node(
        _state("SF1001 的质检为什么没有通过？应该怎么处理？", role="customer_service")
    )

    assert result["terminal_status"] == "SUCCESS"
    assert "不合格 20 个" in result["result"]
    assert "无权访问制造质检 SOP" in result["result"]
    assert "不合格品应隔离并标识" not in result["result"]
    assert result["sop_source_ids"] == []


def test_mixed_query_sop_failure_is_safe_and_read_only(monkeypatch):
    monkeypatch.setattr(
        nodes_module,
        "_retrieve_reranked",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )

    result = nodes_module.quality_mixed_node(
        _state("SF1001 的质检为什么没有通过？应该怎么处理？")
    )

    assert result["terminal_status"] == "SAFE_FALLBACK"
    assert result["safe_fallback_reason"] == "SOP_RETRIEVAL_UNAVAILABLE"
    assert "不合格 20 个" in result["result"]
    assert "未根据缺失资料推测处理方式" in result["result"]


def test_mixed_chat_and_stream_expose_separate_sources(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPPORTFLOW_DATABASE_PATH", str(tmp_path / "api.sqlite"))
    monkeypatch.setattr(nodes_module, "_retrieve_reranked", _mixed_sop)
    client = TestClient(api)
    payload = {
        "message": "SF1001 的质检为什么没有通过？应该怎么处理？",
        "thread_id": "b4-2-api",
        "role": "manager",
    }

    response = client.post("/chat", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "quality_mixed"
    assert body["trace"]["database_source_ids"]
    assert body["trace"]["sop_source_ids"] == ["mfg_demo_quality_sop_v1"]

    stream = client.post("/chat/stream", json={**payload, "thread_id": "b4-2-stream"})
    assert stream.status_code == 200
    assert '"database_source_ids"' in stream.text
    assert '"sop_source_ids"' in stream.text

