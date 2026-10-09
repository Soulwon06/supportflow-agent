"""Auditable local retrieval pipeline for the SupportFlow knowledge base."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from .observability import emit_progress
from .rag_models import (
    CorpusName,
    RagChunk,
    RagDocument,
    filter_documents_for_role,
)


class CorpusUnavailableError(RuntimeError):
    """Raised when an explicitly requested Corpus is not available."""

    def __init__(self, corpus: CorpusName, reason: str):
        self.corpus = corpus
        self.reason = reason
        super().__init__(f"{corpus.value}: {reason}")


@dataclass
class CorpusResources:
    """Independent sparse/vector resources for one Corpus and role."""

    corpus: CorpusName
    role: str
    documents: list[dict[str, Any]]
    tokenized_documents: list[list[str]]
    bm25: BM25Okapi
    document_vectors: Any = None


CorpusContract = RagDocument | RagChunk


DATA_PATH = Path(__file__).parent.parent / "data" / "knowledge_base.json"


def load_knowledge_base() -> list[dict[str, Any]]:
    with open(DATA_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def document_text(document: dict[str, Any]) -> str:
    """Support the accepted legacy ``text`` field and V1.2 ``content``."""
    return str(document.get("content") or document.get("text") or "")


def index_text(document: dict[str, Any]) -> str:
    title = str(document.get("title") or "")
    keywords = " ".join(str(item) for item in document.get("keywords", []))
    return f"{title} {document_text(document)} {keywords}".strip()


def tokenize(text: str) -> list[str]:
    return [token.strip() for token in jieba.lcut(text) if token.strip()]


_retriever_module_started = time.perf_counter()
_knowledge_base_load_started = time.perf_counter()
documents = load_knowledge_base()
_knowledge_base_load_ms = (time.perf_counter() - _knowledge_base_load_started) * 1000
_bm25_initialization_started = time.perf_counter()
tokenized_documents = [tokenize(index_text(document)) for document in documents]
bm25 = BM25Okapi(tokenized_documents)
_bm25_initialization_ms = (time.perf_counter() - _bm25_initialization_started) * 1000
_retriever_initialization_ms = (time.perf_counter() - _retriever_module_started) * 1000

_retriever_startup_profile = {
    "retriever_initialization_ms": round(_retriever_initialization_ms, 3),
    "knowledge_base_loading_ms": round(_knowledge_base_load_ms, 3),
    "bm25_initialization_ms": round(_bm25_initialization_ms, 3),
    "document_count": len(documents),
    "lifecycle": "process_startup",
}


def retriever_startup_profile() -> dict[str, Any]:
    """Return measured one-time initialization timings for this process."""
    return dict(_retriever_startup_profile)


def _legacy_customer_support_contracts() -> list[RagDocument]:
    """Adapt the frozen legacy KB to the B1 contract without changing it."""

    return [
        RagDocument(
            corpus=CorpusName.customer_support,
            document_id=str(document["id"]),
            source_version=str(document.get("version") or "legacy"),
            title=str(document.get("title") or document["id"]),
            content=document_text(document),
            source_section=str(document.get("category") or ""),
            synthetic=False,
            authority="legacy_customer_support",
            allowed_roles=[],
        )
        for document in documents
    ]


_corpus_source_documents: dict[CorpusName, list[CorpusContract]] = {
    CorpusName.customer_support: _legacy_customer_support_contracts(),
}
_corpus_resource_cache: dict[tuple[CorpusName, str], CorpusResources] = {}


def _contract_to_retriever_document(item: CorpusContract) -> dict[str, Any]:
    """Convert a validated contract to the legacy scorer's document shape."""

    if isinstance(item, RagChunk):
        item_id = item.chunk_id
        chunk_id = item.chunk_id
    else:
        item_id = item.document_id
        chunk_id = None
    return {
        "id": item_id,
        "document_id": item.document_id,
        "chunk_id": chunk_id,
        "corpus": item.corpus.value,
        "source_version": item.source_version,
        "version": item.source_version,
        "title": item.title,
        "content": item.content,
        "text": item.content,
        "category": item.source_section,
        "source_section": item.source_section,
        "synthetic": item.synthetic,
        "authority": item.authority,
        "allowed_roles": list(item.allowed_roles),
        "effective_status": item.effective_status.value,
        "keywords": [],
    }


def build_corpus_resources(
    corpus: CorpusName,
    documents_for_corpus: list[CorpusContract],
    *,
    role: str,
) -> CorpusResources:
    """Build isolated BM25 resources after deterministic access filtering.

    This factory is intentionally usable with a small in-memory list for tests
    and B3 preparation.  It does not register a production Corpus or create a
    knowledge-base file.
    """

    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    normalized_role = role.strip()
    visible = filter_documents_for_role(
        documents_for_corpus,
        corpus=corpus,
        role=normalized_role,
    )
    if not visible:
        raise CorpusUnavailableError(corpus, "NO_AUTHORIZED_ACTIVE_DOCUMENTS")
    retriever_documents = [_contract_to_retriever_document(item) for item in visible]
    tokenized = [tokenize(index_text(document)) for document in retriever_documents]
    return CorpusResources(
        corpus=corpus,
        role=normalized_role,
        documents=retriever_documents,
        tokenized_documents=tokenized,
        bm25=BM25Okapi(tokenized),
    )


def corpus_status(corpus: CorpusName) -> dict[str, Any]:
    """Report explicit availability without falling back to another Corpus."""

    if not isinstance(corpus, CorpusName):
        raise ValueError("corpus must be an explicit CorpusName")
    source = _corpus_source_documents.get(corpus)
    if not source:
        return {
            "corpus": corpus.value,
            "available": False,
            "reason": "CORPUS_NOT_CONFIGURED",
        }
    return {
        "corpus": corpus.value,
        "available": True,
        "document_count": len(source),
    }


def _get_corpus_resources(corpus: CorpusName, role: str) -> CorpusResources:
    if not isinstance(corpus, CorpusName):
        raise ValueError("corpus must be an explicit CorpusName")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    if corpus not in _corpus_source_documents:
        raise CorpusUnavailableError(corpus, "CORPUS_NOT_CONFIGURED")
    cache_key = (corpus, role.strip())
    if cache_key not in _corpus_resource_cache:
        _corpus_resource_cache[cache_key] = build_corpus_resources(
            corpus,
            _corpus_source_documents[corpus],
            role=role.strip(),
        )
    return _corpus_resource_cache[cache_key]


def _set_profile(profile: dict[str, Any] | None, name: str, started: float) -> None:
    if profile is not None:
        profile[name] = round((time.perf_counter() - started) * 1000, 3)


def _result(
    document: dict[str, Any],
    score: float,
    method: str,
    rank: int | None = None,
) -> dict[str, Any]:
    """Return a stable evidence shape for tools, RAG prompts, and evaluation."""
    return {
        "id": document["id"],
        "corpus": document.get("corpus", CorpusName.customer_support.value),
        "document_id": document.get("document_id", document["id"]),
        "chunk_id": document.get("chunk_id"),
        # ``text`` remains for compatibility with the accepted knowledge node.
        "text": document_text(document),
        "content": document_text(document),
        "title": document.get("title", ""),
        "category": document.get("category", ""),
        "source_section": document.get("source_section", document.get("category", "")),
        "source_version": document.get("version", ""),
        "synthetic": document.get("synthetic", False),
        "authority": document.get("authority", "legacy_customer_support"),
        "allowed_roles": list(document.get("allowed_roles", [])),
        "effective_status": document.get("effective_status", "active"),
        "score": float(score),
        "rank": rank,
        "retrieval_method": method,
    }


def keyword_search(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """Legacy character-overlap baseline, retained for comparison."""
    results = []
    for document in documents:
        score = sum(char in index_text(document) for char in query)
        if score > 0:
            results.append(_result(document, score, "char_keyword"))
    results.sort(key=lambda item: item["score"], reverse=True)
    return [dict(item, rank=index) for index, item in enumerate(results[:top_k], 1)]


def bm25_search(
    query: str,
    top_k: int = 3,
    *,
    profile: dict[str, Any] | None = None,
    resources: CorpusResources | None = None,
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    active_bm25 = resources.bm25 if resources is not None else bm25
    active_documents = resources.documents if resources is not None else documents
    scores = active_bm25.get_scores(tokenize(query))
    results = [
        _result(document, score, "bm25")
        for document, score in zip(active_documents, scores)
        if score > 0
    ]
    results.sort(key=lambda item: item["score"], reverse=True)
    _set_profile(profile, "bm25_search_ms", started)
    return [dict(item, rank=index) for index, item in enumerate(results[:top_k], 1)]


def cosine_similarity(vector_a, vector_b) -> float:
    dot_product = sum(a * b for a, b in zip(vector_a, vector_b))
    magnitude_a = sum(a * a for a in vector_a) ** 0.5
    magnitude_b = sum(b * b for b in vector_b) ** 0.5
    if magnitude_a == 0 or magnitude_b == 0:
        return 0.0
    return dot_product / (magnitude_a * magnitude_b)


embedding_model = None
document_vectors = None
_dense_load_error: str | None = None
_dense_model_initialization_ms: float | None = None
_kb_embedding_preparation_ms: float | None = None


def _dense_enabled() -> bool:
    return os.getenv("SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ensure_dense_resources(
    profile: dict[str, Any] | None = None,
    *,
    resources: CorpusResources | None = None,
) -> bool:
    global embedding_model, document_vectors, _dense_load_error
    global _dense_model_initialization_ms, _kb_embedding_preparation_ms

    if resources is not None:
        if resources.document_vectors is not None:
            if profile is not None:
                profile.setdefault("dense_model_initialization_ms", 0.0)
                profile.setdefault("kb_embedding_preparation_ms", 0.0)
                profile["dense_cache_hit"] = True
                profile["dense_corpus"] = resources.corpus.value
            return True
        if not _dense_enabled() or _dense_load_error is not None:
            return False
    elif embedding_model is not None and document_vectors is not None:
        if profile is not None:
            profile.setdefault("dense_model_initialization_ms", 0.0)
            profile.setdefault("kb_embedding_preparation_ms", 0.0)
            profile["dense_cache_hit"] = True
        return True

    if not _dense_enabled() or _dense_load_error is not None:
        return False
    try:
        from sentence_transformers import SentenceTransformer

        if embedding_model is None:
            emit_progress("retrieval", "running", "正在加载语义检索模型…")
            model_started = time.perf_counter()
            embedding_model = SentenceTransformer(
                os.getenv(
                    "SUPPORTFLOW_EMBEDDING_MODEL",
                    "paraphrase-multilingual-MiniLM-L12-v2",
                ),
                device=os.getenv("SUPPORTFLOW_EMBEDDING_DEVICE", "cpu"),
                local_files_only=True,
            )
            _set_profile(profile, "dense_model_initialization_ms", model_started)
            _dense_model_initialization_ms = round(
                (time.perf_counter() - model_started) * 1000,
                3,
            )

        if resources is not None:
            emit_progress("retrieval", "running", "正在准备知识库向量…")
            embedding_started = time.perf_counter()
            resources.document_vectors = embedding_model.encode(
                [index_text(document) for document in resources.documents]
            )
            _set_profile(profile, "kb_embedding_preparation_ms", embedding_started)
            if profile is not None:
                profile["dense_cache_hit"] = False
                profile["dense_corpus"] = resources.corpus.value
            return True

        emit_progress("retrieval", "running", "正在准备知识库向量…")
        embedding_started = time.perf_counter()
        document_vectors = embedding_model.encode(
            [index_text(document) for document in documents]
        )
        _set_profile(profile, "kb_embedding_preparation_ms", embedding_started)
        _kb_embedding_preparation_ms = round(
            (time.perf_counter() - embedding_started) * 1000,
            3,
        )
        if profile is not None:
            profile["dense_cache_hit"] = False
        return True
    except Exception as exc:
        _dense_load_error = str(exc)
        return False


def dense_status() -> dict[str, Any]:
    if not _dense_enabled():
        return {
            "enabled": False,
            "available": False,
            "model_ready": False,
            "kb_embeddings_ready": False,
            "error": "disabled",
        }
    available = _ensure_dense_resources()
    return {
        "enabled": True,
        "available": available,
        "model_ready": embedding_model is not None,
        "kb_embeddings_ready": document_vectors is not None,
        "model_initialization_ms": _dense_model_initialization_ms,
        "kb_embedding_preparation_ms": _kb_embedding_preparation_ms,
        "error": None if available else _dense_load_error,
    }


def dense_search(
    query: str,
    top_k: int = 3,
    *,
    profile: dict[str, Any] | None = None,
    resources: CorpusResources | None = None,
) -> list[dict[str, Any]]:
    if not _ensure_dense_resources(profile, resources=resources):
        return []

    emit_progress("retrieval", "running", "正在进行语义检索…")
    query_started = time.perf_counter()
    query_vector = embedding_model.encode(query)
    _set_profile(profile, "query_embedding_ms", query_started)
    similarity_started = time.perf_counter()
    results = []
    active_documents = resources.documents if resources is not None else documents
    active_vectors = resources.document_vectors if resources is not None else document_vectors
    for document, document_vector in zip(active_documents, active_vectors):
        results.append(
            _result(
                document,
                cosine_similarity(query_vector, document_vector),
                "dense",
            )
        )
    results.sort(key=lambda item: item["score"], reverse=True)
    _set_profile(profile, "dense_similarity_search_ms", similarity_started)
    return [dict(item, rank=index) for index, item in enumerate(results[:top_k], 1)]


def reciprocal_rank_fusion(
    sparse_results: list[dict[str, Any]],
    dense_results: list[dict[str, Any]],
    k: int = 60,
    *,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    scores: dict[str, float] = {}
    documents_by_id: dict[str, dict[str, Any]] = {}
    for results in (sparse_results, dense_results):
        for rank, result in enumerate(results, start=1):
            doc_id = result["id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1 / (k + rank)
            documents_by_id[doc_id] = result

    fused = []
    for doc_id, score in scores.items():
        item = dict(documents_by_id[doc_id])
        item["id"] = doc_id
        item["score"] = score
        item["retrieval_method"] = "rrf"
        fused.append(item)
    fused.sort(key=lambda item: item["score"], reverse=True)
    _set_profile(profile, "rrf_fusion_ms", started)
    return [dict(item, rank=index) for index, item in enumerate(fused, 1)]


def hybrid_search(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    return hybrid_candidates(query, candidate_k=top_k)[:top_k]


def hybrid_candidates(
    query: str,
    candidate_k: int = 5,
    *,
    require_dense: bool = False,
    profile: dict[str, Any] | None = None,
    resources: CorpusResources | None = None,
) -> list[dict[str, Any]]:
    """Build the frozen Hybrid/RRF candidate set before reranking.

    Evaluation scripts preserve the historical environment-controlled mode.
    The production safe-RAG path passes ``require_dense=True`` so it cannot
    silently degrade to BM25-only retrieval; local-only model loading still
    keeps startup lazy and prevents Hugging Face network access.
    """
    if profile is not None:
        profile.update(
            {
                "retriever_initialization_ms": 0.0,
                "knowledge_base_loading_ms": 0.0,
                "bm25_initialization_ms": 0.0,
                "startup_profile": retriever_startup_profile(),
            }
        )
    sparse_results = bm25_search(
        query,
        top_k=candidate_k,
        profile=profile,
        resources=resources,
    )
    if require_dense and not _ensure_dense_resources(profile, resources=resources):
        raise RuntimeError("DENSE_RETRIEVAL_UNAVAILABLE")
    dense_results = dense_search(
        query,
        top_k=candidate_k,
        profile=profile,
        resources=resources,
    )
    fused = reciprocal_rank_fusion(sparse_results, dense_results, profile=profile)
    preparation_started = time.perf_counter()
    candidates = fused[:candidate_k]
    _set_profile(profile, "candidate_preparation_ms", preparation_started)
    return candidates


def mock_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    # Deliberately unchanged in behavior: this is a traceable pass-through.
    results = [dict(candidate, retrieval_method="mock_rerank") for candidate in candidates]
    return [dict(item, rank=index) for index, item in enumerate(results[:top_k], 1)]


_real_reranker = None
_real_reranker_path: str | None = None
_real_reranker_device: str | None = None
_real_reranker_initialization_seconds: float | None = None


def load_real_reranker(model_path: str | Path, device: str = "cpu"):
    """Load a local Cross-Encoder reranker without any network fallback."""
    global _real_reranker
    global _real_reranker_path
    global _real_reranker_device
    global _real_reranker_initialization_seconds

    path = Path(model_path)
    if not path.is_dir():
        raise FileNotFoundError(f"Local reranker directory not found: {path}")
    if _real_reranker is not None:
        if _real_reranker_path == str(path) and _real_reranker_device == device:
            return _real_reranker
        raise RuntimeError("A different local reranker is already loaded")

    from sentence_transformers import CrossEncoder

    started = time.perf_counter()
    _real_reranker = CrossEncoder(
        str(path),
        device=device,
        local_files_only=True,
    )
    _real_reranker_initialization_seconds = time.perf_counter() - started
    _real_reranker_path = str(path)
    _real_reranker_device = str(_real_reranker.device)
    return _real_reranker


def real_reranker_status() -> dict[str, Any]:
    return {
        "loaded": _real_reranker is not None,
        "model_path": _real_reranker_path,
        "device": _real_reranker_device,
        "initialization_seconds": _real_reranker_initialization_seconds,
    }


def real_rerank(
    query: str,
    candidates: list[dict[str, Any]],
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Score and reorder an existing candidate set with the loaded local model."""
    if _real_reranker is None:
        raise RuntimeError("Call load_real_reranker() before real_rerank()")
    if not candidates:
        return []

    pairs = [
        [query, str(candidate.get("content") or candidate.get("text") or "")]
        for candidate in candidates
    ]
    started = time.perf_counter()
    scores = _real_reranker.predict(pairs, show_progress_bar=False)
    latency_ms = (time.perf_counter() - started) * 1000

    scored = []
    for candidate, score in zip(candidates, scores):
        item = dict(candidate)
        item.setdefault("source_id", item.get("id"))
        item["original_rank"] = candidate.get("rank")
        item["rerank_score"] = float(score)
        item["rerank_latency_ms"] = round(latency_ms, 3)
        item["rerank_method"] = "bge-reranker-v2-m3"
        scored.append(item)

    scored.sort(
        key=lambda item: (
            -item["rerank_score"],
            item["original_rank"] if item["original_rank"] is not None else 10**9,
        )
    )
    return [
        dict(item, rerank_rank=index, rank=index)
        for index, item in enumerate(scored[:top_k], 1)
    ]


def retrieve(query: str, candidate_k: int = 5, top_k: int = 3) -> list[dict[str, Any]]:
    candidates = hybrid_candidates(query, candidate_k=candidate_k)
    return mock_rerank(query, candidates, top_k=top_k)


def retrieve_with_real_reranker(
    query: str,
    candidate_k: int = 5,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Controlled-experiment path: frozen Hybrid/RRF followed by real reranking."""
    candidates = hybrid_candidates(query, candidate_k=candidate_k)
    return real_rerank(query, candidates, top_k=top_k)


def retrieve_from_corpus(
    query: str,
    *,
    corpus: CorpusName,
    role: str,
    candidate_k: int = 5,
    top_k: int = 3,
    require_dense: bool = True,
    use_reranker: bool = True,
    resources: CorpusResources | None = None,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve from an explicitly selected, role-filtered Corpus.

    The existing ``retrieve`` and ``hybrid_candidates`` functions remain the
    compatibility path for the historical customer-support KB.  New callers
    must pass ``corpus`` and ``role``.  A missing manufacturing Corpus raises
    ``CorpusUnavailableError``; it never falls back to customer support.
    """

    if not isinstance(corpus, CorpusName):
        raise ValueError("corpus must be an explicit CorpusName")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")
    normalized_role = role.strip()
    if resources is None:
        resources = _get_corpus_resources(corpus, normalized_role)
    elif resources.corpus is not corpus or resources.role != normalized_role:
        raise ValueError("resources do not match the requested corpus and role")

    candidates = hybrid_candidates(
        query,
        candidate_k=candidate_k,
        require_dense=require_dense,
        profile=profile,
        resources=resources,
    )
    if not candidates:
        return []
    if use_reranker:
        return real_rerank(query, candidates, top_k=top_k)
    return [dict(item, rank=index) for index, item in enumerate(candidates[:top_k], 1)]

