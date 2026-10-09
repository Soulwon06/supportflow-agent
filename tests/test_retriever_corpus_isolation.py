import pytest

from app import retriever
from app.rag_models import CorpusName, EffectiveStatus, RagDocument
from app.retriever import (
    CorpusUnavailableError,
    build_corpus_resources,
    corpus_status,
    retrieve,
    retrieve_from_corpus,
)


def doc(
    corpus: CorpusName,
    document_id: str,
    content: str,
    *,
    allowed_roles: list[str] | None = None,
    effective_status: EffectiveStatus = EffectiveStatus.active,
) -> RagDocument:
    return RagDocument(
        corpus=corpus,
        document_id=document_id,
        source_version="test-1.0",
        title=document_id,
        content=content,
        source_section="test",
        synthetic=True,
        authority="test_only",
        allowed_roles=allowed_roles or [],
        effective_status=effective_status,
    )


@pytest.fixture
def bm25_only(monkeypatch):
    """Keep isolation tests away from real Dense model execution."""

    monkeypatch.setattr(retriever, "_dense_enabled", lambda: False)


def test_legacy_customer_support_interface_still_works(bm25_only):
    legacy = retrieve("退款需要几天", candidate_k=5, top_k=3)

    assert legacy
    assert all(item["corpus"] == CorpusName.customer_support.value for item in legacy)
    assert all(item["document_id"] for item in legacy)


def test_explicit_customer_support_corpus_returns_only_customer_documents(bm25_only):
    results = retrieve_from_corpus(
        "退款需要几天",
        corpus=CorpusName.customer_support,
        role="employee",
        candidate_k=5,
        top_k=3,
        require_dense=False,
        use_reranker=False,
    )

    assert results
    assert {item["corpus"] for item in results} == {"customer_support"}
    assert all(item["chunk_id"] is None for item in results)


def test_manufacturing_corpus_missing_fails_without_customer_fallback():
    status = corpus_status(CorpusName.manufacturing_demo)
    assert status == {
        "corpus": "manufacturing_demo",
        "available": False,
        "reason": "CORPUS_NOT_CONFIGURED",
    }

    with pytest.raises(CorpusUnavailableError, match="CORPUS_NOT_CONFIGURED"):
        retrieve_from_corpus(
            "生产前需要检查什么",
            corpus=CorpusName.manufacturing_demo,
            role="production_employee",
            require_dense=False,
            use_reranker=False,
        )


def test_in_memory_corpora_are_isolated_and_do_not_mix(bm25_only):
    support_resources = build_corpus_resources(
        CorpusName.customer_support,
        [
            doc(CorpusName.customer_support, "support-001", "refund_policy_001"),
            doc(CorpusName.customer_support, "support-002", "account_help_002"),
            doc(CorpusName.customer_support, "support-003", "shipping_help_003"),
        ],
        role="employee",
    )
    manufacturing_resources = build_corpus_resources(
        CorpusName.manufacturing_demo,
        [
            doc(CorpusName.manufacturing_demo, "mfg-001", "production_check_001"),
            doc(CorpusName.manufacturing_demo, "mfg-002", "packing_check_002"),
            doc(CorpusName.manufacturing_demo, "mfg-003", "quality_check_003"),
        ],
        role="production_employee",
    )

    support_results = retrieve_from_corpus(
        "refund_policy_001",
        corpus=CorpusName.customer_support,
        role="employee",
        resources=support_resources,
        require_dense=False,
        use_reranker=False,
    )
    manufacturing_results = retrieve_from_corpus(
        "production_check_001",
        corpus=CorpusName.manufacturing_demo,
        role="production_employee",
        resources=manufacturing_resources,
        require_dense=False,
        use_reranker=False,
    )

    assert support_results[0]["document_id"] == "support-001"
    assert manufacturing_results[0]["document_id"] == "mfg-001"
    assert all(item["corpus"] == "customer_support" for item in support_results)
    assert all(item["corpus"] == "manufacturing_demo" for item in manufacturing_results)
    assert support_resources.bm25 is not manufacturing_resources.bm25
    assert support_resources.documents is not manufacturing_resources.documents
    assert support_resources.document_vectors is None
    assert manufacturing_resources.document_vectors is None


def test_role_and_effective_status_filter_before_indexing(bm25_only):
    resources = build_corpus_resources(
        CorpusName.manufacturing_demo,
        [
            doc(
                CorpusName.manufacturing_demo,
                "allowed",
                "allowed_unique_production_check",
                allowed_roles=["production_employee"],
            ),
            doc(
                CorpusName.manufacturing_demo,
                "allowed-other",
                "allowed_unique_packaging_check",
                allowed_roles=["production_employee"],
            ),
            doc(
                CorpusName.manufacturing_demo,
                "allowed-third",
                "allowed_unique_inventory_check",
                allowed_roles=["production_employee"],
            ),
            doc(
                CorpusName.manufacturing_demo,
                "forbidden",
                "forbidden_unique_production_check",
                allowed_roles=["quality_employee"],
            ),
            doc(
                CorpusName.manufacturing_demo,
                "old",
                "superseded_unique_production_check",
                allowed_roles=["production_employee"],
                effective_status=EffectiveStatus.superseded,
            ),
        ],
        role="production_employee",
    )

    assert [item["document_id"] for item in resources.documents] == [
        "allowed",
        "allowed-other",
        "allowed-third",
    ]
    results = retrieve_from_corpus(
        "production_check",
        corpus=CorpusName.manufacturing_demo,
        role="production_employee",
        resources=resources,
        require_dense=False,
        use_reranker=False,
    )
    assert [item["document_id"] for item in results] == ["allowed"]


def test_dense_unavailable_is_explicit_failure(monkeypatch):
    resources = build_corpus_resources(
        CorpusName.manufacturing_demo,
        [doc(CorpusName.manufacturing_demo, "mfg-001", "生产前检查")],
        role="production_employee",
    )
    monkeypatch.setattr(retriever, "_dense_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="DENSE_RETRIEVAL_UNAVAILABLE"):
        retrieve_from_corpus(
            "生产前检查",
            corpus=CorpusName.manufacturing_demo,
            role="production_employee",
            resources=resources,
            require_dense=True,
            use_reranker=False,
        )


def test_dense_vectors_are_cached_per_corpus_resource_without_real_model(monkeypatch):
    support_resources = build_corpus_resources(
        CorpusName.customer_support,
        [doc(CorpusName.customer_support, "support-001", "退款说明")],
        role="employee",
    )
    manufacturing_resources = build_corpus_resources(
        CorpusName.manufacturing_demo,
        [doc(CorpusName.manufacturing_demo, "mfg-001", "生产说明")],
        role="production_employee",
    )

    class FakeEmbeddingModel:
        def encode(self, values):
            return [[float(len(value))] for value in values]

    monkeypatch.setattr(retriever, "_dense_enabled", lambda: True)
    monkeypatch.setattr(retriever, "_dense_load_error", None)
    monkeypatch.setattr(retriever, "embedding_model", FakeEmbeddingModel())
    monkeypatch.setattr(retriever, "document_vectors", None)

    assert retriever._ensure_dense_resources(resources=support_resources)
    assert retriever._ensure_dense_resources(resources=manufacturing_resources)
    assert support_resources.document_vectors is not None
    assert manufacturing_resources.document_vectors is not None
    assert support_resources.document_vectors is not manufacturing_resources.document_vectors
    assert len(support_resources.document_vectors) == 1
    assert len(manufacturing_resources.document_vectors) == 1


def test_evidence_provenance_fields_survive_hybrid_retrieval(bm25_only):
    resources = build_corpus_resources(
        CorpusName.manufacturing_demo,
        [
            doc(
                CorpusName.manufacturing_demo,
                "mfg-001",
                "packing_requirement_001",
            ),
            doc(
                CorpusName.manufacturing_demo,
                "mfg-002",
                "quality_check_002",
            ),
            doc(
                CorpusName.manufacturing_demo,
                "mfg-003",
                "outbound_check_003",
            ),
        ],
        role="warehouse_employee",
    )

    result = retrieve_from_corpus(
        "packing_requirement_001",
        corpus=CorpusName.manufacturing_demo,
        role="warehouse_employee",
        resources=resources,
        require_dense=False,
        use_reranker=False,
    )[0]

    assert result["corpus"] == "manufacturing_demo"
    assert result["document_id"] == "mfg-001"
    assert result["chunk_id"] is None
    assert result["source_version"] == "test-1.0"
    assert result["source_section"] == "test"
    assert result["effective_status"] == "active"
