import pytest

from app import retriever
from app.rag_models import CorpusName, RagDocument, validate_unique_ids
from app.retriever import CorpusUnavailableError, retrieve_from_corpus


GOLD_CASES = [
    ("生产前设备物料工单核对怎么做", "mfg_demo_production_sop_v1", "production_employee"),
    ("不合格品应该怎样隔离和复检", "mfg_demo_quality_sop_v1", "quality_employee"),
    ("卷轴包装要检查哪些批次标签", "mfg_demo_reel_packaging_v1", "warehouse_employee"),
    ("出库前需要核对什么并怎样交接", "mfg_demo_outbound_sop_v1", "warehouse_employee"),
    ("订单报工质检出库分别由谁审批", "mfg_demo_approval_workflow_v1", "admin"),
]


def test_manufacturing_demo_corpus_has_five_valid_documents():
    status = retriever.corpus_status(CorpusName.manufacturing_demo)
    assert status == {
        "corpus": "manufacturing_demo",
        "available": True,
        "document_count": 5,
    }

    documents = retriever._corpus_source_documents[CorpusName.manufacturing_demo]
    assert len(documents) == 5
    assert all(isinstance(item, RagDocument) for item in documents)
    assert all(item.corpus is CorpusName.manufacturing_demo for item in documents)
    assert all(item.synthetic is True for item in documents)
    assert all(item.authority == "teaching_demo_only" for item in documents)
    assert all(item.effective_status.value == "active" for item in documents)
    validate_unique_ids(documents)


def test_invalid_manufacturing_corpus_is_reported_without_fallback(monkeypatch):
    monkeypatch.setattr(retriever, "_manufacturing_demo_load_error", "CORPUS_INVALID")

    status = retriever.corpus_status(CorpusName.manufacturing_demo)
    assert status["available"] is False
    assert status["reason"] == "CORPUS_INVALID"

    with pytest.raises(CorpusUnavailableError, match="CORPUS_INVALID"):
        retrieve_from_corpus(
            "生产前核对",
            corpus=CorpusName.manufacturing_demo,
            role="production_employee",
            require_dense=False,
            use_reranker=False,
        )


@pytest.mark.parametrize("query, expected_document_id, role", GOLD_CASES)
def test_gold_queries_retrieve_the_manually_defined_source(
    query, expected_document_id, role
):
    results = retrieve_from_corpus(
        query,
        corpus=CorpusName.manufacturing_demo,
        role=role,
        candidate_k=5,
        top_k=5,
        require_dense=False,
        use_reranker=False,
    )

    assert results
    assert results[0]["document_id"] == expected_document_id
    assert results[0]["corpus"] == CorpusName.manufacturing_demo.value


def test_manufacturing_role_filter_excludes_quality_document_for_sales_employee():
    results = retrieve_from_corpus(
        "不合格品 隔离 复检",
        corpus=CorpusName.manufacturing_demo,
        role="sales_employee",
        candidate_k=5,
        top_k=5,
        require_dense=False,
        use_reranker=False,
    )

    assert all(item["document_id"] != "mfg_demo_quality_sop_v1" for item in results)


def test_manufacturing_and_customer_queries_remain_separate():
    manufacturing_results = retrieve_from_corpus(
        "不合格品 隔离 复检",
        corpus=CorpusName.manufacturing_demo,
        role="quality_employee",
        require_dense=False,
        use_reranker=False,
    )
    customer_results = retrieve_from_corpus(
        "退款需要几天",
        corpus=CorpusName.customer_support,
        role="employee",
        require_dense=False,
        use_reranker=False,
    )

    assert manufacturing_results
    assert customer_results
    assert all(item["corpus"] == "manufacturing_demo" for item in manufacturing_results)
    assert all(item["corpus"] == "customer_support" for item in customer_results)


def test_manufacturing_evidence_keeps_document_and_section_provenance():
    result = retrieve_from_corpus(
        "出库前核对和交接",
        corpus=CorpusName.manufacturing_demo,
        role="warehouse_employee",
        require_dense=False,
        use_reranker=False,
    )[0]

    assert result["document_id"] == "mfg_demo_outbound_sop_v1"
    assert result["source_version"] == "demo-1.0"
    assert result["source_section"] == "outbound_handover"
    assert result["synthetic"] is True
    assert result["authority"] == "teaching_demo_only"
    assert result["effective_status"] == "active"
