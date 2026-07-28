"""Idempotent workspace initialization for artifacts, knowledge, and RAG."""

from __future__ import annotations

import logging
from pathlib import Path

from src.utils.app_registry import artifacts_root

logger = logging.getLogger(__name__)

_INITIALIZED = False


def ensure_workspace() -> Path:
    """Create base artifact directories and ensure the Chroma collection exists.

    Idempotent and safe to call on every process start. Does **not** pre-create
    per-app folders (those are created lazily via ``ensure_app_dirs``).

    Returns:
        Absolute path to the artifacts root.
    """
    global _INITIALIZED
    root = artifacts_root()
    for rel in ("k6", "recordings", "knowledge", "rag/chroma"):
        (root / rel).mkdir(parents=True, exist_ok=True)

    try:
        from src.utils.rag_store import ensure_collection

        ensure_collection()
    except Exception as exc:
        logger.warning("RAG/Chroma init skipped: %s", exc)

    if not _INITIALIZED:
        logger.info("Workspace ready under %s", root)
        _INITIALIZED = True
    return root
