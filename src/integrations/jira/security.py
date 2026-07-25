"""Sanitize text before posting to Jira comments."""

from __future__ import annotations

from src.security.secrets import redact_text_for_llm

_MAX_COMMENT_CHARS = 30_000


def sanitize_comment(text: str) -> str:
    """Redact secrets and truncate oversized comment bodies."""
    body = redact_text_for_llm(text or "")
    if len(body) > _MAX_COMMENT_CHARS:
        body = body[: _MAX_COMMENT_CHARS - 20] + "\n\n…(truncated)"
    return body
