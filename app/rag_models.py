"""Strict data contracts and deterministic access gates for RAG corpora.

This module intentionally does not build an index or change the existing
retriever.  B1 only establishes the contract that B2 can use to keep the
customer-support and manufacturing corpora separate.
"""

from __future__ import annotations

from enum import Enum
from typing import Iterable, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CorpusName(str, Enum):
    """Known corpus names; callers must choose one explicitly."""

    customer_support = "customer_support"
    manufacturing_demo = "manufacturing_demo"


class EffectiveStatus(str, Enum):
    """Whether a document is eligible for normal retrieval."""

    active = "active"
    superseded = "superseded"


def _non_empty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("metadata fields must not be empty")
    return value


class RagDocument(BaseModel):
    """Document-level metadata shared by every corpus."""

    model_config = ConfigDict(extra="forbid")

    corpus: CorpusName
    document_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_section: str = ""
    synthetic: bool = False
    authority: str = "unknown"
    allowed_roles: list[str] = Field(default_factory=list)
    effective_status: EffectiveStatus = EffectiveStatus.active

    @field_validator(
        "document_id",
        "source_version",
        "title",
        "content",
        "source_section",
        "authority",
        mode="before",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_empty(value) if isinstance(value, str) else value

    @field_validator("allowed_roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        cleaned = [_non_empty(role) for role in value]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_roles must be unique")
        return cleaned


class RagChunk(BaseModel):
    """Stable, retrievable unit derived from one :class:`RagDocument`."""

    model_config = ConfigDict(extra="forbid")

    corpus: CorpusName
    document_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    source_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_section: str = ""
    synthetic: bool = False
    authority: str = "unknown"
    allowed_roles: list[str] = Field(default_factory=list)
    effective_status: EffectiveStatus = EffectiveStatus.active

    @field_validator(
        "document_id",
        "chunk_id",
        "source_version",
        "title",
        "content",
        "source_section",
        "authority",
        mode="before",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_empty(value) if isinstance(value, str) else value

    @field_validator("chunk_id")
    @classmethod
    def validate_chunk_prefix(cls, value: str, info) -> str:
        document_id = info.data.get("document_id")
        if document_id and not value.startswith(f"{document_id}#"):
            raise ValueError("chunk_id must start with document_id followed by '#'")
        return value

    @field_validator("allowed_roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        cleaned = [_non_empty(role) for role in value]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_roles must be unique")
        return cleaned


class RetrievalEvidence(BaseModel):
    """Evidence returned to Judge/Generator with provenance preserved."""

    model_config = ConfigDict(extra="forbid")

    corpus: CorpusName
    document_id: str = Field(min_length=1)
    chunk_id: str | None = None
    source_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_section: str = ""
    synthetic: bool = False
    authority: str = "unknown"
    allowed_roles: list[str] = Field(default_factory=list)
    effective_status: EffectiveStatus = EffectiveStatus.active
    retrieval_method: str = Field(min_length=1)
    original_rank: int | None = Field(default=None, ge=1)
    rerank_score: float | None = None
    rerank_rank: int | None = Field(default=None, ge=1)

    @field_validator(
        "document_id",
        "source_version",
        "title",
        "content",
        "source_section",
        "authority",
        "retrieval_method",
        mode="before",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return _non_empty(value) if isinstance(value, str) else value

    @field_validator("chunk_id")
    @classmethod
    def validate_chunk_prefix(cls, value: str | None, info) -> str | None:
        if value is None:
            return value
        document_id = info.data.get("document_id")
        if document_id and not value.startswith(f"{document_id}#"):
            raise ValueError("chunk_id must start with document_id followed by '#'")
        return value

    @field_validator("allowed_roles")
    @classmethod
    def validate_roles(cls, value: list[str]) -> list[str]:
        cleaned = [_non_empty(role) for role in value]
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_roles must be unique")
        return cleaned


ModelT = TypeVar("ModelT", RagDocument, RagChunk, RetrievalEvidence)


def validate_unique_ids(items: Iterable[ModelT]) -> list[ModelT]:
    """Validate stable IDs and return the materialized items unchanged.

    Documents must have unique document IDs.  Chunks and evidence may share a
    document ID, but every explicit chunk ID must be unique within the batch.
    Mixed Corpus batches are rejected so a future index cannot silently merge
    customer-support and manufacturing records.
    """

    materialized = list(items)
    corpora = {item.corpus for item in materialized}
    if len(corpora) > 1:
        raise ValueError("a corpus batch must contain exactly one corpus")

    document_ids: set[str] = set()
    chunk_ids: set[str] = set()
    for item in materialized:
        if isinstance(item, RagDocument):
            if item.document_id in document_ids:
                raise ValueError(f"duplicate document_id: {item.document_id}")
            document_ids.add(item.document_id)
        chunk_id = getattr(item, "chunk_id", None)
        if chunk_id is not None:
            if chunk_id in chunk_ids:
                raise ValueError(f"duplicate chunk_id: {chunk_id}")
            chunk_ids.add(chunk_id)
    return materialized


def filter_documents_for_role(
    items: Iterable[ModelT],
    *,
    corpus: CorpusName,
    role: str,
) -> list[ModelT]:
    """Apply deterministic Corpus, lifecycle, and role filtering.

    ``corpus`` is keyword-only and has no default on purpose: a future
    manufacturing retrieval call cannot silently fall back to the historical
    customer-support corpus.  An empty ``allowed_roles`` means the document is
    general internal knowledge; a non-empty list is an allowlist.
    """

    if not isinstance(corpus, CorpusName):
        raise ValueError("corpus must be an explicit CorpusName")
    if not isinstance(role, str) or not role.strip():
        raise ValueError("role must be a non-empty string")

    # Filter the requested Corpus before validating the batch.  A caller may
    # receive a mixed upstream collection, but the returned candidate set must
    # never contain a different Corpus and must still pass ID validation.
    requested_corpus_items = [item for item in items if item.corpus == corpus]

    visible: list[ModelT] = []
    for item in validate_unique_ids(requested_corpus_items):
        if item.effective_status is not EffectiveStatus.active:
            continue
        if item.allowed_roles and role not in item.allowed_roles:
            continue
        visible.append(item)
    return visible
