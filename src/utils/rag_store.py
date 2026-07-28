"""Local ChromaDB RAG over markdown knowledge (soft-fail when unavailable)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.app_registry import artifacts_root

logger = logging.getLogger(__name__)

COLLECTION_NAME = "nfe_knowledge"
_CHUNK_SIZE = 800
_CHUNK_OVERLAP = 80

_client = None
_collection = None
_unavailable_reason: Optional[str] = None


class _HashEmbeddingFunction:
    """Deterministic local embedding for tests (no ONNX / network)."""

    @staticmethod
    def name() -> str:
        return "nfe_hash_embedding"

    def embed_query(self, input: List[str]) -> List[List[float]]:
        return self(input)

    def __call__(self, input: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for text in input:
            vec = [0.0] * 32
            for i, ch in enumerate((text or "").encode("utf-8")):
                vec[i % 32] += (ch % 31) / 31.0
            # L2 normalize
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out

    def get_config(self) -> Dict[str, Any]:
        return {"type": "nfe_hash_embedding"}

    @staticmethod
    def build_from_config(config: Dict[str, Any]) -> "_HashEmbeddingFunction":
        return _HashEmbeddingFunction()


def _use_fake_embeddings() -> bool:
    import os

    return os.getenv("NFE_RAG_FAKE_EMBEDDINGS", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _embedding_function():
    if _use_fake_embeddings():
        return _HashEmbeddingFunction()
    return None  # Chroma default ONNX embedding


def _rag_enabled() -> bool:
    try:
        from config.settings import settings

        return bool(settings.NFE_RAG_ENABLED)
    except Exception:
        return True


def _top_k_default() -> int:
    try:
        from config.settings import settings

        return max(1, int(settings.NFE_RAG_TOP_K or 4))
    except Exception:
        return 4


def chroma_path() -> Path:
    """Persistent Chroma directory under ``artifacts/rag/chroma``."""
    path = artifacts_root() / "rag" / "chroma"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _knowledge_jail() -> Path:
    root = artifacts_root() / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _assert_knowledge_path(path: str) -> Optional[Path]:
    """Return resolved path only when it stays under ``artifacts/knowledge/``."""
    if not path:
        return None
    try:
        from src.security.fs_jail import assert_under_jail

        return assert_under_jail(Path(path), _knowledge_jail())
    except Exception:
        return None


def chunk_markdown(text: str, *, chunk_size: int = _CHUNK_SIZE) -> List[str]:
    """Split markdown on ``##`` headings / ~chunk_size chars with small overlap."""
    raw = (text or "").strip()
    if not raw:
        return []
    # Prefer heading-based sections
    parts = re.split(r"(?m)(?=^##\s+)", raw)
    chunks: List[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(part) <= chunk_size:
            chunks.append(part)
            continue
        start = 0
        while start < len(part):
            end = min(len(part), start + chunk_size)
            chunks.append(part[start:end])
            if end >= len(part):
                break
            start = max(0, end - _CHUNK_OVERLAP)
    return chunks or [raw[:chunk_size]]


def get_client():
    """Return a persistent Chroma client, or ``None`` on soft-fail."""
    global _client, _unavailable_reason
    if not _rag_enabled():
        _unavailable_reason = "NFE_RAG_ENABLED=false"
        return None
    if _unavailable_reason and _client is None:
        return None
    if _client is not None:
        return _client
    try:
        import chromadb

        _client = chromadb.PersistentClient(path=str(chroma_path()))
        return _client
    except Exception as exc:
        _unavailable_reason = str(exc)
        logger.warning("ChromaDB unavailable (RAG disabled): %s", exc)
        return None


def ensure_collection():
    """Ensure the ``nfe_knowledge`` collection exists (empty OK). Soft-fails."""
    global _collection
    client = get_client()
    if client is None:
        return None
    try:
        ef = _embedding_function()
        if ef is not None:
            _collection = client.get_or_create_collection(
                name=COLLECTION_NAME,
                embedding_function=ef,
            )
        else:
            _collection = client.get_or_create_collection(name=COLLECTION_NAME)
        # Rebuild from disk when collection is empty but markdown exists
        try:
            count = _collection.count()
        except Exception:
            count = 0
        if count == 0:
            knowledge = artifacts_root() / "knowledge"
            if knowledge.is_dir():
                for app_dir in knowledge.iterdir():
                    if app_dir.is_dir():
                        try:
                            reindex_app(app_dir.name)
                        except Exception as exc:
                            logger.debug("reindex_app(%s) skipped: %s", app_dir.name, exc)
        return _collection
    except Exception as exc:
        logger.warning("Chroma ensure_collection failed: %s", exc)
        return None


def _get_collection():
    global _collection
    if _collection is not None:
        return _collection
    return ensure_collection()


def _doc_id(app: str, kind: str, flow_or_overview: str) -> str:
    flow = flow_or_overview or "overview"
    return f"{app}::{kind}::{flow}"


def upsert_markdown(
    app: str,
    *,
    flow: str = "",
    kind: str = "flow",
    text: str = "",
    path: str = "",
) -> int:
    """Chunk and upsert markdown into Chroma. Returns number of chunks written.

    Soft-fails (returns 0) when RAG is disabled or Chroma is unavailable.
    Only indexes content whose ``path`` is under ``artifacts/knowledge/``.
    """
    if not _rag_enabled():
        return 0
    if not app or not (text or "").strip():
        return 0
    if path:
        jailed = _assert_knowledge_path(path)
        if jailed is None:
            logger.warning("Refusing to index path outside knowledge jail: %s", path)
            return 0
        path = str(jailed)

    collection = _get_collection()
    if collection is None:
        return 0

    chunks = chunk_markdown(text)
    if not chunks:
        return 0

    base_id = _doc_id(app, kind, flow or "overview")
    ids = [f"{base_id}::c{i}" for i in range(len(chunks))]
    now = datetime.now(timezone.utc).isoformat()
    metadatas = [
        {
            "app": app,
            "flow": flow or "",
            "kind": kind,
            "path": path or "",
            "updated_at": now,
            "chunk": i,
        }
        for i in range(len(chunks))
    ]
    try:
        # Drop prior chunks for this doc (stable upsert across chunk-count changes)
        try:
            existing = collection.get(where={"app": app})
            stale = [
                eid
                for eid, meta in zip(
                    existing.get("ids") or [],
                    existing.get("metadatas") or [],
                )
                if str((meta or {}).get("kind")) == kind
                and str((meta or {}).get("flow") or "") == (flow or "")
            ]
            if stale:
                collection.delete(ids=stale)
        except Exception:
            pass
        collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        return len(chunks)
    except Exception as exc:
        logger.warning("Chroma upsert failed: %s", exc)
        return 0


def query(
    text: str,
    *,
    app: Optional[str] = None,
    top_k: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Query knowledge chunks. Returns ``[{text, metadata, distance}, ...]``.

    Soft-fails to an empty list when RAG is disabled or Chroma is unavailable.
    """
    if not _rag_enabled() or not (text or "").strip():
        return []
    collection = _get_collection()
    if collection is None:
        return []
    n = top_k if top_k is not None else _top_k_default()
    kwargs: Dict[str, Any] = {
        "query_texts": [text],
        "n_results": max(1, n),
    }
    if app:
        kwargs["where"] = {"app": app}
    try:
        result = collection.query(**kwargs)
    except Exception as exc:
        logger.warning("Chroma query failed: %s", exc)
        return []

    docs = (result.get("documents") or [[]])[0]
    metas = (result.get("metadatas") or [[]])[0]
    dists = (result.get("distances") or [[]])[0]
    rows: List[Dict[str, Any]] = []
    for doc, meta, dist in zip(docs, metas, dists):
        rows.append(
            {
                "text": doc or "",
                "metadata": dict(meta or {}),
                "distance": dist,
            }
        )
    return rows


def reindex_app(app: str) -> int:
    """Rebuild Chroma entries for one app from on-disk markdown. Returns chunk count."""
    if not app or not _rag_enabled():
        return 0
    app_dir = artifacts_root() / "knowledge" / app
    if not app_dir.is_dir():
        return 0
    total = 0
    overview = app_dir / "overview.md"
    if overview.is_file():
        total += upsert_markdown(
            app,
            flow="",
            kind="overview",
            text=overview.read_text(encoding="utf-8"),
            path=str(overview),
        )
    flows = app_dir / "flows"
    if flows.is_dir():
        for path in flows.glob("*.md"):
            total += upsert_markdown(
                app,
                flow=path.stem,
                kind="flow",
                text=path.read_text(encoding="utf-8"),
                path=str(path),
            )
    return total


def reset_client_for_tests() -> None:
    """Clear cached client/collection (unit tests only)."""
    global _client, _collection, _unavailable_reason
    _client = None
    _collection = None
    _unavailable_reason = None
