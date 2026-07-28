"""Sanitize text before posting to Confluence pages."""

from __future__ import annotations

from src.security.secrets import redact_text_for_llm

_MAX_BODY_CHARS = 100_000


def sanitize_storage(text: str) -> str:
    """Redact secrets and truncate oversized Confluence storage bodies."""
    body = redact_text_for_llm(text or "")
    if len(body) > _MAX_BODY_CHARS:
        body = body[: _MAX_BODY_CHARS - 20] + "\n<!-- truncated -->"
    return body


def sanitize_title(title: str, *, max_len: int = 180) -> str:
    """Strip unsafe characters and truncate Confluence page titles."""
    cleaned = (title or "").strip()
    for ch in ("[", "]", "{", "}", "|", "/", "\\", "<", ">"):
        cleaned = cleaned.replace(ch, " ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = "Untitled flow"
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned
