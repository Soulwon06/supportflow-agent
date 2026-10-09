import pytest
from pydantic import ValidationError

from app.rag_models import (
    CorpusName,
    EffectiveStatus,
    RagChunk,
    RagDocument,
    RetrievalEvidence,
    filter_documents_for_role,
    validate_unique_ids,
)
from app.retriever import DATA_PATH, load_knowledge_base


def document(**overrides) -> RagDocument:
    payload = {
        "corpus": CorpusName.manufacturing_demo,
        "document_id": "mfg-sop-001",
        "source_version": "demo-1.0",
        "title": "生产前检查",
        "content": "开始生产前完成设备和物料检查。",
        "source_section": "开机前",
        "synthetic": True,
        "authority": "teaching_demo_only",
        "allowed_roles": ["production_employee"],
        "effective_status": EffectiveStatus.active,
    }
    payload.update(overrides)
    return RagDocument.model_validate(payload)


def chunk(**overrides) -> RagChunk:
    payload = {
        "corpus": CorpusName.manufacturing_demo,
        "document_id": "mfg-sop-001",
        "chunk_id": "mfg-sop-001#chunk-001",
        "source_version": "demo-1.0",
        "title": "生产前检查",
        "content": "开始生产前完成设备和物料检查。",
        "source_section": "开机前",
        "synthetic": True,
        "authority": "teaching_demo_only",
        "allowed_roles": ["production_employee"],
        "effective_status": EffectiveStatus.active,
    }
    payload.update(overrides)
    return RagChunk.model_validate(payload)


def evidence(**overrides) -> RetrievalEvidence:
    payload = {
        "corpus": CorpusName.manufacturing_demo,
        "document_id": "mfg-sop-001",
        "chunk_id": "mfg-sop-001#chunk-001",
        "source_version": "demo-1.0",
        "title": "生产前检查",
        "content": "开始生产前完成设备和物料检查。",
        "source_section": "开机前",
        "synthetic": True,
        "authority": "teaching_demo_only",
        "allowed_roles": ["production_employee"],
        "effective_status": EffectiveStatus.active,
        "retrieval_method": "bm25",
        "original_rank": 1,
        "rerank_score": 0.8,
        "rerank_rank": 1,
    }
    payload.update(overrides)
    return RetrievalEvidence.model_validate(payload)


def test_corpus_and_status_are_closed_enums():
    assert CorpusName("customer_support") is CorpusName.customer_support
    assert EffectiveStatus("superseded") is EffectiveStatus.superseded

    with pytest.raises(ValidationError):
        document(corpus="unknown")
    with pytest.raises(ValidationError):
        document(effective_status="deleted")


def test_required_metadata_and_extra_fields_are_rejected():
    with pytest.raises(ValidationError):
        RagDocument(corpus=CorpusName.manufacturing_demo)
    with pytest.raises(ValidationError):
        document(unexpected_field="must be rejected")


def test_chunk_and_evidence_ids_are_stable_and_tied_to_document():
    assert chunk().chunk_id == "mfg-sop-001#chunk-001"
    assert evidence().chunk_id == "mfg-sop-001#chunk-001"

    with pytest.raises(ValidationError):
        chunk(chunk_id="other-document#chunk-001")
    with pytest.raises(ValidationError):
        evidence(chunk_id="other-document#chunk-001")

    with pytest.raises(ValueError, match="duplicate document_id"):
        validate_unique_ids([document(), document()])
    with pytest.raises(ValueError, match="duplicate chunk_id"):
        validate_unique_ids([chunk(), chunk(chunk_id="mfg-sop-001#chunk-001")])


def test_superseded_documents_are_not_active_candidates():
    item = document(effective_status=EffectiveStatus.superseded)
    assert filter_documents_for_role(
        [item], corpus=CorpusName.manufacturing_demo, role="production_employee"
    ) == []


def test_role_access_is_deterministic_and_wrong_corpus_is_filtered():
    allowed = document(document_id="allowed")
    public = document(document_id="public", allowed_roles=[])
    forbidden = document(document_id="forbidden", allowed_roles=["quality_employee"])
    customer_support = document(
        document_id="support",
        corpus=CorpusName.customer_support,
        allowed_roles=[],
    )

    visible = filter_documents_for_role(
        [allowed, public, forbidden, customer_support],
        corpus=CorpusName.manufacturing_demo,
        role="production_employee",
    )
    assert [item.document_id for item in visible] == ["allowed", "public"]


def test_corpus_is_required_and_cannot_fallback_implicitly():
    with pytest.raises(ValueError, match="explicit CorpusName"):
        filter_documents_for_role([document()], corpus=None, role="production_employee")


def test_retrieval_evidence_preserves_provenance_fields():
    item = evidence()
    assert item.corpus is CorpusName.manufacturing_demo
    assert item.document_id == "mfg-sop-001"
    assert item.source_version == "demo-1.0"
    assert item.original_rank == 1
    assert item.rerank_rank == 1


def test_historical_customer_support_retriever_interface_remains_unchanged():
    items = load_knowledge_base()
    assert DATA_PATH.name == "knowledge_base.json"
    assert len(items) == 40
    assert {item["category"] for item in items} >= {
        "refund_return",
        "logistics_shipping",
    }
