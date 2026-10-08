"""Explicit manual smoke test; never run as part of the normal pytest suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow the documented `python scripts/smoke_test_deepseek.py` invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings
from app.llm_client import ProviderError, build_provider
from app.llm_service import LLMService


def main() -> int:
    settings = get_settings()
    if not settings.enabled:
        print("SKIPPED: missing SUPPORTFLOW_OPENAI_API_KEY or unsupported provider configuration")
        return 0

    service = LLMService(build_provider(settings))
    try:
        for message in ("退款政策是什么？", "商品有质量问题应该怎么处理？"):
            result = service.classify_intent(message)
            print(f"{message} -> {result.intent.value}")
            if result.intent.value != "knowledge":
                print("FAILED: unexpected intent classification")
                return 1
    except ProviderError as exc:
        print(f"FAILED: provider error classification={exc.classification}")
        return 1

    usage = service.last_response.usage if service.last_response else {}
    print("PASS: DeepSeek HTTP call, JSON parsing, and intent schema validation succeeded")
    print(f"usage={usage}")
    print("API key was not printed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
