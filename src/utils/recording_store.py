"""Persist Watch-me captures so analysis can rerun without re-recording."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DIR = _PROJECT_ROOT / "artifacts" / "recordings"


def recordings_dir() -> Path:
    """Resolve the directory for saved Watch-me JSON captures."""
    import os

    override = os.getenv("NFE_RECORDINGS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_DIR


def _slug_host(target_url: str) -> str:
    """Filesystem-safe host stem from a URL."""
    try:
        host = urlparse(target_url or "").netloc or "recording"
    except Exception:
        host = "recording"
    host = re.sub(r"[^a-zA-Z0-9._-]+", "_", host).strip("._") or "recording"
    return host[:60]


def save_watch_me_recording(
    *,
    target_url: str,
    user_journey_steps: List[Any],
    run_records: List[Dict[str, Any]],
    credentials: Optional[Dict[str, str]] = None,
    sub_tasks: Optional[List[Dict[str, Any]]] = None,
    label: str = "",
) -> Dict[str, str]:
    """Write a reusable Watch-me capture (stable path per host).

    Args:
        target_url: Journey start URL.
        user_journey_steps: Recorded Playwright steps.
        run_records: One or two capture runs (network + timeline).
        credentials: Optional login credentials used for the flow.
        sub_tasks: Optional sub-task metadata.
        label: Optional human label stored in the JSON.

    Returns:
        Metadata with ``path``, ``filename``, ``relative_path``.
    """
    out_dir = recordings_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    host = _slug_host(target_url)
    filename = f"{host}.json"
    path = out_dir / filename

    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "label": label or host,
        "source": "watch_me",
        "target_url": target_url,
        "credentials": dict(credentials or {}),
        "user_journey_steps": user_journey_steps or [],
        "sub_tasks": sub_tasks or [],
        "run_records": run_records or [],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    abs_path = str(path.resolve())
    logger.info(
        "Saved Watch-me recording → %s (%s steps, %s run(s))",
        abs_path,
        len(user_journey_steps or []),
        len(run_records or []),
    )
    rel = (
        str(path.relative_to(_PROJECT_ROOT))
        if path.is_relative_to(_PROJECT_ROOT)
        else abs_path
    )
    return {
        "path": abs_path,
        "filename": filename,
        "relative_path": rel,
        "host": host,
    }


def list_recordings(limit: int = 20) -> List[Dict[str, Any]]:
    """List saved recordings (newest first by mtime)."""
    out_dir = recordings_dir()
    if not out_dir.is_dir():
        return []
    rows: List[Dict[str, Any]] = []
    for path in sorted(out_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        rows.append(
            {
                "path": str(path.resolve()),
                "filename": path.name,
                "relative_path": (
                    str(path.relative_to(_PROJECT_ROOT))
                    if path.is_relative_to(_PROJECT_ROOT)
                    else str(path)
                ),
                "target_url": data.get("target_url") or "",
                "saved_at": data.get("saved_at") or "",
                "steps": len(data.get("user_journey_steps") or []),
                "runs": len(data.get("run_records") or []),
                "label": data.get("label") or path.stem,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def resolve_recording_path(hint: str = "") -> Optional[Path]:
    """Resolve a recording path from a user hint (path, host, or empty=latest)."""
    out_dir = recordings_dir()
    text = (hint or "").strip().strip("`\"'")

    if text:
        direct = Path(text).expanduser()
        if direct.is_file():
            return direct.resolve()
        # relative to project or recordings dir
        for candidate in (
            _PROJECT_ROOT / text,
            out_dir / text,
            out_dir / f"{text}.json",
            out_dir / f"{_slug_host(text)}.json",
        ):
            if candidate.is_file():
                return candidate.resolve()
        # host substring match
        host = _slug_host(text) if "://" in text else text
        matches = [
            p
            for p in out_dir.glob("*.json")
            if host.lower() in p.stem.lower() or host.lower() in p.name.lower()
        ]
        if matches:
            return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]

    listed = list_recordings(limit=1)
    if listed:
        return Path(listed[0]["path"])
    return None


def load_watch_me_recording(path: Union[Path, str]) -> Dict[str, Any]:
    """Load a saved recording JSON into agent state fields.

    Args:
        path: Absolute or relative path to a recording file.

    Returns:
        Mapping suitable for merging into ``AgentState``.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If JSON is invalid or missing required fields.
    """
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Recording not found: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Recording JSON must be an object")
    target_url = str(data.get("target_url") or "").strip()
    steps = data.get("user_journey_steps") or []
    runs = data.get("run_records") or []
    if not target_url:
        raise ValueError("Recording is missing target_url")
    if not steps and not runs:
        raise ValueError("Recording has no steps or run_records")
    return {
        "target_url": target_url,
        "credentials": dict(data.get("credentials") or {}),
        "user_journey_steps": steps,
        "sub_tasks": data.get("sub_tasks") or [],
        "run_records": runs,
        "recording_mode": "reuse",
        "watch_me_status": "loaded",
        "recording_file": str(file_path),
    }


def format_recordings_list(rows: List[Dict[str, Any]]) -> str:
    """Markdown list of saved recordings for chat."""
    if not rows:
        return (
            "No saved Watch-me recordings yet.\n\n"
            "Record once with **watch me &lt;url&gt;**, then reuse with "
            "**analyse saved recording**."
        )
    lines = ["## Saved Watch-me recordings", ""]
    for i, row in enumerate(rows, 1):
        lines.append(
            f"{i}. `{row.get('relative_path') or row.get('filename')}` — "
            f"{row.get('steps', 0)} steps, {row.get('runs', 0)} run(s) — "
            f"{row.get('target_url') or '(no url)'}"
        )
    lines.append("")
    lines.append(
        "Reuse: **analyse saved recording** "
        "(or `analyse saved recording <host-or-path>`)."
    )
    lines.append("")
    return "\n".join(lines)
