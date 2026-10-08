import json
import logging
import re
from datetime import datetime, timezone
from typing import Any


_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)(Bearer\s+)[^\s,;]+"),
    re.compile(
        r"(?i)(SUPPORTFLOW_OPENAI_API_KEY\s*[=:]\s*)[^\s,;}\"']+"
    ),
    re.compile(
        r"(?i)([\"']?api[_-]?key[\"']?\s*[:=]\s*[\"']?)[^,\s;}\"']+"
    ),
    re.compile(r"(?i)\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}\b"),
)


def _redact_sensitive_text(value: str) -> str:
    """Redact common API-key and Authorization representations in logs."""

    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(
                lambda match: f"{match.group(1)}[REDACTED]",
                redacted,
            )
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _redact_sensitive_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_sensitive_text(value)
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in {
                "api_key",
                "apikey",
                "authorization",
            }:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_sensitive_value(item)
        return redacted
    if isinstance(value, (list, tuple)):
        return [_redact_sensitive_value(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """
    把 LogRecord 转换成一行 JSON。
    """

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_sensitive_text(record.getMessage()),
        }

        # SupportFlow 自定义字段
        supportflow_fields = [
            "event",
            "trace_id",
            "thread_id",
            "success",
            "error_code",
            "total_latency_ms",
            "ttfe_ms",
            "llm_calls",
            "llm_usage",
            "llm_operations",
        ]

        for field_name in supportflow_fields:
            if hasattr(record, field_name):
                log_data[field_name] = _redact_sensitive_value(
                    getattr(record, field_name)
                )

        if record.exc_info:
            log_data["exception"] = _redact_sensitive_text(
                self.formatException(record.exc_info)
            )

        return json.dumps(
            _redact_sensitive_value(log_data),
            ensure_ascii=False,
        )


def configure_logging() -> None:
    """
    配置 SupportFlow 的结构化日志。
    """

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    root_logger = logging.getLogger()

    root_logger.setLevel(logging.INFO)

    # 避免 reload 时重复添加 Handler。
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
