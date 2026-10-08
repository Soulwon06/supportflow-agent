"""Provider orchestration, strict parsing, and observability for LLM calls."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .llm_client import LLMProvider, ProviderError
from .llm_models import AnswerabilityJudgment, GroundedAnswer, IntentClassification
from .observability import get_current_trace
from .prompts import (
    GROUNDED_GENERATION_SYSTEM_PROMPT,
    ANSWERABILITY_JUDGE_SYSTEM_PROMPT,
    build_answerability_judge_user_prompt,
    build_constrained_grounded_user_prompt,
    INTENT_SYSTEM_PROMPT,
    build_grounded_user_prompt,
    build_intent_user_prompt,
)


def _parse_json_object(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    try:
        data = json.loads(cleaned)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("INVALID_JSON", "LLM content was not valid JSON") from exc
    if not isinstance(data, dict):
        raise ProviderError("INVALID_JSON", "LLM JSON content was not an object")
    return data


class LLMService:
    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self.last_response = None

    def classify_intent(self, message: str) -> IntentClassification:
        response = self._call(
            operation="intent_classification",
            system_prompt=INTENT_SYSTEM_PROMPT,
            user_prompt=build_intent_user_prompt(message),
        )
        try:
            return IntentClassification.model_validate(_parse_json_object(response.content))
        except ValidationError as exc:
            self._mark_last_span_failure("SCHEMA_VALIDATION", str(exc))
            raise ProviderError("SCHEMA_VALIDATION", "invalid intent schema") from exc

    def generate_grounded(self, message: str, evidence: list[dict]) -> GroundedAnswer:
        return self.generate_constrained_grounded(
            message,
            evidence,
            status="SUPPORTED",
            supported_facts=[],
            missing_facts=[],
        )

    def judge_answerability(
        self,
        message: str,
        evidence_text: str,
    ) -> AnswerabilityJudgment:
        response = self._call(
            operation="answerability_judge",
            system_prompt=ANSWERABILITY_JUDGE_SYSTEM_PROMPT,
            user_prompt=build_answerability_judge_user_prompt(message, evidence_text),
        )
        try:
            return AnswerabilityJudgment.model_validate(_parse_json_object(response.content))
        except ValidationError as exc:
            self._mark_last_span_failure("SCHEMA_VALIDATION", str(exc))
            raise ProviderError("SCHEMA_VALIDATION", "invalid answerability schema") from exc

    def generate_constrained_grounded(
        self,
        message: str,
        evidence: list[dict],
        *,
        status: str,
        supported_facts: list[str],
        missing_facts: list[str],
    ) -> GroundedAnswer:
        response = self._call(
            operation="grounded_generation",
            system_prompt=GROUNDED_GENERATION_SYSTEM_PROMPT,
            user_prompt=build_constrained_grounded_user_prompt(
                message,
                evidence,
                status=status,
                supported_facts=supported_facts,
                missing_facts=missing_facts,
            ),
        )
        try:
            return GroundedAnswer.model_validate(_parse_json_object(response.content))
        except ValidationError as exc:
            self._mark_last_span_failure("SCHEMA_VALIDATION", str(exc))
            raise ProviderError("SCHEMA_VALIDATION", "invalid grounded answer schema") from exc

    def _call(self, *, operation: str, system_prompt: str, user_prompt: str):
        trace = get_current_trace()
        span = None
        if trace is not None:
            span = trace.start_span(
                "llm",
                metadata={
                    "provider": getattr(self.provider, "provider_name", "unknown"),
                    "model": getattr(self.provider, "model", ""),
                    "operation": operation,
                },
            )
        try:
            response = self.provider.complete_json(
                operation=operation,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self.last_response = response
            if span is not None:
                span.metadata["usage"] = response.usage
                span.finish(success=True)
                trace.record_llm_usage(response.usage)
            return response
        except ProviderError as exc:
            if span is not None:
                span.metadata["error_classification"] = exc.classification
                span.finish(success=False, error=exc.classification)
            raise
        except Exception as exc:
            if span is not None:
                span.metadata["error_classification"] = "UNEXPECTED_PROVIDER_ERROR"
                span.finish(success=False, error="UNEXPECTED_PROVIDER_ERROR")
            raise ProviderError("UNEXPECTED_PROVIDER_ERROR", str(exc)) from exc

    def _mark_last_span_failure(self, classification: str, error: str) -> None:
        trace = get_current_trace()
        if trace is None or not trace.spans:
            return
        span = trace.spans[-1]
        span.metadata["error_classification"] = classification
        span.finish(success=False, error=error)
