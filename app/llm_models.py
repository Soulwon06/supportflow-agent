"""Strict schemas used at the boundary between an LLM and the workflow."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class IntentName(str, Enum):
    knowledge = "knowledge"
    order = "order"
    logistics = "logistics"
    refund = "refund"
    inventory = "inventory"
    production = "production"
    order_entry = "order_entry"
    unknown = "unknown"


class IntentClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: IntentName


class GroundedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)


class AnswerabilityStatus(str, Enum):
    supported = "SUPPORTED"
    partial = "PARTIAL"
    unsupported = "UNSUPPORTED"


class AnswerabilityJudgment(BaseModel):
    """Strict boundary schema for the evidence sufficiency judge."""

    model_config = ConfigDict(extra="forbid")

    status: AnswerabilityStatus
    supported_facts: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
