import json
import logging

from app.logging_config import JsonFormatter


def test_json_formatter_redacts_credentials_and_authorization() -> None:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "Authorization: Bearer sk-test-secret-12345 "
            "api_key=sk-test-secret-67890"
        ),
        args=(),
        exc_info=None,
    )
    record.llm_operations = [
        {"authorization": "Bearer sk-test-secret-abcde"},
    ]

    rendered = JsonFormatter().format(record)
    payload = json.loads(rendered)

    assert "sk-test-secret" not in rendered
    assert "[REDACTED]" in rendered
    assert payload["llm_operations"][0]["authorization"] == "[REDACTED]"
