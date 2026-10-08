"""Auditable local retrieval pipeline for the SupportFlow knowledge base."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import jieba
from rank_bm25 import BM25Okapi

from .observability import emit_progress


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
        # ``text`` remains for compatibility with the accepted knowledge node.
        "text": document_text(document),
        "content": document_text(document),
        "title": document.get("title", ""),
        "category": document.get("category", ""),
        "source_version": document.get("version", ""),
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
) -> list[dict[str, Any]]:
    started = time.perf_counter()
    scores = bm25.get_scores(tokenize(query))
    results = [
        _result(document, score, "bm25")
        for document, score in zip(documents, scores)
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


def _ensure_dense_resources(profile: dict[str, Any] | None = None) -> bool:
    global embedding_model, document_vectors, _dense_load_error
    global _dense_model_initialization_ms, _kb_embedding_preparation_ms
    if embedding_model is not None and document_vectors is not None:
        if profile is not None:
            profile.setdefault("dense_model_initialization_ms", 0.0)
            profile.setdefault("kb_embedding_preparation_ms", 0.0)
            profile.setdefault("dense_cache_hit", True)
        return True
    if not _dense_enabled() or _dense_load_error is not None:
        return False
    try:
        from sentence_transformers import SentenceTransformer

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
) -> list[dict[str, Any]]:
    if not _ensure_dense_resources(profile):
        return []

    emit_progress("retrieval", "running", "正在进行语义检索…")
    query_started = time.perf_counter()
    query_vector = embedding_model.encode(query)
    _set_profile(profile, "query_embedding_ms", query_started)
    similarity_started = time.perf_counter()
    results = []
    for document, document_vector in zip(documents, document_vectors):
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
    sparse_results = bm25_search(query, top_k=candidate_k, profile=profile)
    if require_dense and not _ensure_dense_resources(profile):
        raise RuntimeError("DENSE_RETRIEVAL_UNAVAILABLE")
    dense_results = dense_search(query, top_k=candidate_k, profile=profile)
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

