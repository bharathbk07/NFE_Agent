"""Lightweight durable memory notes for the PE agent (per app / thread)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.app_registry import artifacts_root


def _memory_dir() -> Path:
    d = artifacts_root() / "api" / "agent_memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_note(thread_id: str, note: str, *, kind: str = "note") -> Path:
    path = _memory_dir() / f"{thread_id or 'default'}.jsonl"
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "note": (note or "")[:4000],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def recent_notes(thread_id: str, *, limit: int = 20) -> List[Dict[str, Any]]:
    path = _memory_dir() / f"{thread_id or 'default'}.jsonl"
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    out: List[Dict[str, Any]] = []
    for line in lines[-max(1, limit) :]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def notes_as_context(thread_id: str, *, limit: int = 12) -> str:
    notes = recent_notes(thread_id, limit=limit)
    if not notes:
        return ""
    parts = ["### Agent memory (recent)"]
    for n in notes:
        parts.append(f"- [{n.get('kind')}] {n.get('note')}")
    return "\n".join(parts)
