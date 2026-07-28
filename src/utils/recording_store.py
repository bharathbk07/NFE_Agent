"""Persist Watch-me captures so analysis can rerun without re-recording."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from src.security.fs_jail import assert_under_jail
from src.security.secrets import credentials_for_storage, redact_run_records, redact_step
from src.exceptions import ErrorCode, NFEValidationError
from config.settings import settings
from src.utils.app_registry import (
    app_id_from_url,
    ensure_app_dirs,
    resolve_app_and_flow,
    slug_flow,
)

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


def recording_app_dir(app_id: str) -> Path:
    """Return ``recordings/<app>/`` (created lazily)."""
    app = (app_id or "").strip()
    if not app:
        return recordings_dir()
    ensure_app_dirs(app)
    return recordings_dir() / app


def save_watch_me_recording(
    *,
    target_url: str,
    user_journey_steps: List[Any],
    run_records: List[Dict[str, Any]],
    credentials: Optional[Dict[str, str]] = None,
    sub_tasks: Optional[List[Dict[str, Any]]] = None,
    label: str = "",
    app: str = "",
    flow: str = "",
) -> Dict[str, str]:
    """Write a reusable Watch-me capture under ``recordings/<app>/<flow>.json``.

    Args:
        target_url: Journey start URL.
        user_journey_steps: Recorded Playwright steps.
        run_records: One or two capture runs (network + timeline).
        credentials: Optional login credentials used for the flow.
        sub_tasks: Optional sub-task metadata.
        label: Optional human label stored in the JSON (also used as flow).
        app: Explicit app id (domain).
        flow: Explicit flow id.

    Returns:
        Metadata with ``path``, ``filename``, ``relative_path``, ``app``, ``flow``.
    """
    app_id, flow_id = resolve_app_and_flow(
        target_url=target_url,
        label=flow or label,
        explicit_app=app,
    )
    if not app_id:
        app_id = app_id_from_url(target_url) or "unknown"
    if not flow_id:
        flow_id = "default"

    out_dir = recording_app_dir(app_id)
    filename = f"{flow_id}.json"
    path = out_dir / filename
    path = assert_under_jail(path, recordings_dir())

    known_secrets = [v for v in (credentials or {}).values() if v]
    steps_out = list(user_journey_steps or [])
    runs_out = list(run_records or [])
    if settings.NFE_REDACT_ARTIFACTS:
        steps_out = [
            redact_step(s, known_secrets=known_secrets) if isinstance(s, dict) else s
            for s in steps_out
        ]
        runs_out = redact_run_records(runs_out, known_secrets=known_secrets)

    display_label = (label or flow or flow_id).strip() or flow_id
    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "label": display_label,
        "app": app_id,
        "flow": flow_id,
        "source": "watch_me",
        "target_url": target_url,
        "credentials": credentials_for_storage(credentials),
        "user_journey_steps": steps_out,
        "sub_tasks": sub_tasks or [],
        "run_records": runs_out,
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
        "host": app_id,
        "app": app_id,
        "flow": flow_id,
    }


def _iter_recording_files(out_dir: Path) -> List[Path]:
    """Collect recording JSON files (app subdirs + legacy flat)."""
    if not out_dir.is_dir():
        return []
    files: List[Path] = []
    # App-scoped: recordings/<app>/<flow>.json
    for path in out_dir.glob("*/*.json"):
        if path.is_file():
            files.append(path)
    # Legacy flat: recordings/<host>.json
    for path in out_dir.glob("*.json"):
        if path.is_file():
            files.append(path)
    return files


def list_recordings(
    limit: int = 20,
    *,
    app: str = "",
) -> List[Dict[str, Any]]:
    """List saved recordings (newest first by mtime).

    Args:
        limit: Maximum rows to return.
        app: Optional app id filter (domain).
    """
    out_dir = recordings_dir()
    if not out_dir.is_dir():
        return []
    app_filter = (app or "").strip().lower()
    rows: List[Dict[str, Any]] = []
    paths = sorted(
        _iter_recording_files(out_dir),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        row_app = str(data.get("app") or "")
        if not row_app and path.parent != out_dir:
            row_app = path.parent.name
        if not row_app:
            row_app = app_id_from_url(str(data.get("target_url") or "")) or path.stem
        row_flow = str(data.get("flow") or path.stem)
        if app_filter and app_filter not in row_app.lower():
            continue
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
                "app": row_app,
                "flow": row_flow,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def resolve_recording_path(
    hint: str = "",
    *,
    app: str = "",
    flow: str = "",
) -> Optional[Path]:
    """Resolve a recording path from a user hint (path, app/flow, or empty=latest).

    Prefers app-scoped paths; falls back to legacy flat ``recordings/*.json``.
    Only paths under ``recordings_dir()`` are accepted (path jail).
    """
    out_dir = recordings_dir()
    text = (hint or "").strip().strip("`\"'")

    def _jailed(candidate: Path) -> Optional[Path]:
        try:
            resolved = assert_under_jail(candidate, out_dir)
        except Exception:
            return None
        return resolved if resolved.is_file() else None

    # Explicit app + flow
    if app and flow:
        flow_slug = slug_flow(flow) or flow
        for candidate in (
            out_dir / app / f"{flow_slug}.json",
            out_dir / f"{flow_slug}.json",
            out_dir / f"{app}.json",
        ):
            jailed = _jailed(candidate)
            if jailed is not None:
                return jailed

    if text:
        direct = Path(text).expanduser()
        if direct.is_file():
            jailed = _jailed(direct)
            if jailed is not None:
                return jailed

        # app/flow path fragment
        if "/" in text and not text.startswith("http"):
            parts = Path(text)
            candidates = [
                out_dir / text,
                out_dir / f"{text}.json",
                out_dir / parts,
            ]
            if len(parts.parts) == 2:
                candidates.append(out_dir / parts.parts[0] / f"{parts.parts[1]}.json")
            for candidate in candidates:
                jailed = _jailed(candidate)
                if jailed is not None:
                    return jailed

        app_from_hint = app_id_from_url(text) if ("://" in text or "." in text) else ""
        flow_from_hint = slug_flow(text) if "://" not in text else ""
        search_app = app or app_from_hint
        search_flow = flow or flow_from_hint

        candidates = []
        if search_app and search_flow:
            candidates.append(out_dir / search_app / f"{search_flow}.json")
        if search_app:
            candidates.append(out_dir / search_app / f"{search_flow or 'default'}.json")
            # Legacy flat by domain
            candidates.append(out_dir / f"{search_app}.json")
        if search_flow:
            candidates.append(out_dir / f"{search_flow}.json")
        candidates.extend(
            [
                out_dir / text,
                out_dir / f"{text}.json",
            ]
        )
        for candidate in candidates:
            jailed = _jailed(candidate)
            if jailed is not None:
                return jailed

        # Substring match across app-scoped + legacy files
        needle = (search_flow or search_app or text).lower()
        matches = [
            p
            for p in _iter_recording_files(out_dir)
            if needle in p.stem.lower()
            or needle in p.name.lower()
            or needle in str(p.parent.name).lower()
        ]
        if matches:
            return sorted(matches, key=lambda p: p.stat().st_mtime, reverse=True)[0]

        # Natural-language RAG hint (soft-fail)
        try:
            from src.utils.rag_store import query as rag_query

            hits = rag_query(text, app=app or None, top_k=3)
            for hit in hits:
                meta = hit.get("metadata") or {}
                hit_app = str(meta.get("app") or "")
                hit_flow = str(meta.get("flow") or "")
                if hit_app and hit_flow:
                    candidate = out_dir / hit_app / f"{hit_flow}.json"
                    jailed = _jailed(candidate)
                    if jailed is not None:
                        return jailed
        except Exception:
            pass

    listed = list_recordings(limit=1, app=app)
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
        NFEValidationError: If the file is missing or JSON is invalid.
        FsJailError: If the path escapes the recordings jail.
    """
    file_path = assert_under_jail(Path(path).expanduser(), recordings_dir())
    if not file_path.is_file():
        raise NFEValidationError(
            f"Recording not found: {file_path}",
            code=ErrorCode.RECORDING_MISSING,
            user_message=f"Recording not found: {file_path}",
        )
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise NFEValidationError(
            "Recording JSON must be an object",
            code=ErrorCode.VALIDATION,
            user_message="Recording file is not valid JSON object.",
        )
    target_url = str(data.get("target_url") or "").strip()
    steps = data.get("user_journey_steps") or []
    runs = data.get("run_records") or []
    if not target_url:
        raise NFEValidationError(
            "Recording is missing target_url",
            code=ErrorCode.VALIDATION,
            user_message="Recording is missing target_url.",
        )
    if not steps and not runs:
        raise NFEValidationError(
            "Recording has no steps or run_records",
            code=ErrorCode.VALIDATION,
            user_message="Recording has no steps or run records.",
        )
    app = str(data.get("app") or "")
    flow = str(data.get("flow") or "")
    if not app and file_path.parent != recordings_dir():
        app = file_path.parent.name
    if not app:
        app = app_id_from_url(target_url)
    if not flow:
        flow = file_path.stem
    return {
        "target_url": target_url,
        "credentials": dict(data.get("credentials") or {}),
        "user_journey_steps": steps,
        "sub_tasks": data.get("sub_tasks") or [],
        "run_records": runs,
        "recording_mode": "reuse",
        "watch_me_status": "loaded",
        "recording_file": str(file_path),
        "app": app,
        "flow": flow,
        "recording_label": data.get("label") or flow,
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
        app_flow = ""
        if row.get("app") or row.get("flow"):
            app_flow = f" [{row.get('app')}/{row.get('flow')}]"
        lines.append(
            f"{i}. `{row.get('relative_path') or row.get('filename')}`{app_flow} — "
            f"{row.get('steps', 0)} steps, {row.get('runs', 0)} run(s) — "
            f"{row.get('target_url') or '(no url)'}"
        )
    lines.append("")
    lines.append(
        "Reuse: **analyse saved recording** "
        "(or `analyse saved recording <app>/<flow>`)."
    )
    lines.append("")
    return "\n".join(lines)
