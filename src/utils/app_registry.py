"""Resolve application (URL domain) and flow ids for artifact paths."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UNSAFE_FS = re.compile(r"[^a-zA-Z0-9._-]+")
_MAX_APP_LEN = 80
_MAX_FLOW_LEN = 60


def artifacts_root() -> Path:
    """Return the project ``artifacts/`` directory."""
    return _PROJECT_ROOT / "artifacts"


def app_id_from_url(url: str) -> str:
    """Derive a filesystem-safe app id from a URL domain (netloc).

    Strips port and a leading ``www.``, then replaces unsafe characters.
    Empty / unparseable URLs yield an empty string (caller may fall back).
    """
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
        host = (parsed.netloc or parsed.path.split("/")[0] or "").strip()
    except Exception:
        return ""
    if not host:
        return ""
    # Drop userinfo and port
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    if ":" in host:
        host = host.split(":", 1)[0]
    host = host.strip(".").lower()
    if host.startswith("www."):
        host = host[4:]
    host = _UNSAFE_FS.sub("_", host).strip("._")
    if not host:
        return ""
    return host[:_MAX_APP_LEN]


def slug_flow(label: str) -> str:
    """Filesystem-safe flow name from a Watch-me label / recording stem."""
    text = (label or "").strip()
    if not text:
        return ""
    # Drop path / extension if a filename was passed
    stem = Path(text).stem if ("/" in text or "\\" in text or text.endswith(".json")) else text
    stem = stem.strip().lower()
    stem = re.sub(r"\s+", "-", stem)
    stem = _UNSAFE_FS.sub("_", stem).strip("._-")
    if not stem:
        return ""
    return stem[:_MAX_FLOW_LEN]


def flow_from_url_path(url: str) -> str:
    """Short path-based flow slug, or ``default`` when none is useful."""
    text = (url or "").strip()
    if not text:
        return "default"
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
        parts = [p for p in (parsed.path or "").split("/") if p]
    except Exception:
        return "default"
    # Skip common auth/login noise; prefer last meaningful segment
    skip = {"web", "index.php", "auth", "login", "api", "v1", "v2"}
    useful = [p for p in parts if p.lower() not in skip and not p.endswith(".php")]
    if not useful:
        return "default"
    return slug_flow(useful[-1]) or "default"


def resolve_app_id(
    *,
    target_url: str = "",
    explicit_url: str = "",
    explicit_app: str = "",
    default_app: str = "",
) -> str:
    """Resolve app id: URL domain → explicit domain string → default.

    ``explicit_app`` is accepted only when it already looks like a domain
    (contains a dot) — friendly names like ``orange-hrm`` are ignored.
    """
    for candidate in (target_url, explicit_url):
        app = app_id_from_url(candidate)
        if app:
            return app
    raw = (explicit_app or "").strip()
    if raw and "." in raw:
        # Treat as a domain-like string (may lack scheme)
        app = app_id_from_url(raw if "://" in raw else f"https://{raw}")
        if app:
            return app
    fallback = (default_app or "").strip()
    if fallback:
        # Default may be a bare domain from env
        app = app_id_from_url(fallback if "://" in fallback else f"https://{fallback}")
        if app:
            return app
        return slug_flow(fallback) or fallback[:_MAX_APP_LEN]
    return ""


def resolve_flow_id(
    *,
    label: str = "",
    recording_hint: str = "",
    target_url: str = "",
) -> str:
    """Resolve flow id: label → recording stem → path slug → ``default``."""
    for candidate in (label, recording_hint):
        flow = slug_flow(candidate)
        if flow:
            return flow
    return flow_from_url_path(target_url) or "default"


def resolve_app_and_flow(
    *,
    target_url: str = "",
    label: str = "",
    recording_hint: str = "",
    explicit_app: str = "",
    default_app: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(app_id, flow_id)`` using locked resolution order."""
    if default_app is None:
        try:
            from config.settings import settings

            default_app = settings.NFE_DEFAULT_APP
        except Exception:
            default_app = ""
    app = resolve_app_id(
        target_url=target_url,
        explicit_app=explicit_app,
        default_app=default_app or "",
    )
    flow = resolve_flow_id(
        label=label,
        recording_hint=recording_hint,
        target_url=target_url,
    )
    return app, flow


def extract_watch_me_label(text: str) -> str:
    """Extract an optional flow label from a watch-me chat message.

    Examples:
      ``watch me create-claim https://example.com`` → ``create-claim``
      ``watch me "Create Claim" https://...`` → ``Create Claim``
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    m = re.search(
        r"\bwatch[\s_-]*me\b\s+(?:as\s+|for\s+|label\s+)?[\"']?([^\"'\n]+?)[\"']?\s+(https?://\S+)",
        cleaned,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(
        r"\bwatch[\s_-]*me\b\s+[\"']([^\"']+)[\"']",
        cleaned,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return ""


def ensure_app_dirs(app_id: str) -> Path:
    """Create per-app artifact folders lazily; seed ``overview.md`` if missing.

    Returns:
        Absolute path to ``knowledge/<app_id>/``.
    """
    app = (app_id or "").strip()
    if not app:
        raise ValueError("app_id is required")
    # Re-sanitize in case callers pass raw host
    app = app_id_from_url(f"https://{app}") or slug_flow(app) or app
    root = artifacts_root()
    knowledge_dir = root / "knowledge" / app
    flows_dir = knowledge_dir / "flows"
    flows_dir.mkdir(parents=True, exist_ok=True)

    # Prefer configured k6 / recordings bases when overridden via env.
    try:
        import os

        k6_base = os.getenv("NFE_ARTIFACTS_DIR", "").strip()
        k6_root = Path(k6_base).expanduser().resolve() if k6_base else root / "k6"
        (k6_root / app).mkdir(parents=True, exist_ok=True)
    except Exception:
        (root / "k6" / app).mkdir(parents=True, exist_ok=True)

    try:
        import os

        rec_base = os.getenv("NFE_RECORDINGS_DIR", "").strip()
        rec_root = Path(rec_base).expanduser().resolve() if rec_base else root / "recordings"
        (rec_root / app).mkdir(parents=True, exist_ok=True)
    except Exception:
        (root / "recordings" / app).mkdir(parents=True, exist_ok=True)

    overview = knowledge_dir / "overview.md"
    if not overview.is_file():
        overview.write_text(
            f"# {app}\n\n"
            f"Application knowledge base for `{app}`.\n\n"
            "Flow cards live under `flows/`.\n",
            encoding="utf-8",
        )
        logger.info("Seeded knowledge overview → %s", overview)
        try:
            from src.utils.rag_store import upsert_markdown

            upsert_markdown(
                app,
                flow="",
                kind="overview",
                text=overview.read_text(encoding="utf-8"),
                path=str(overview),
            )
        except Exception as exc:
            logger.debug("Overview RAG upsert skipped: %s", exc)
    return knowledge_dir
