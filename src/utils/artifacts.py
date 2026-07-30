"""Persist generated artifacts (k6 scripts, IR) to disk for download."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.security.fs_jail import assert_under_jail, safe_artifact_filename
from src.exceptions import ErrorCode, NFEValidationError
from src.utils.app_registry import (
    ensure_app_dirs,
    resolve_app_and_flow,
    slug_flow,
)

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DIR = _PROJECT_ROOT / "artifacts" / "k6"

# Legacy timestamped names: host_YYYYMMDD_HHMMSS(.js|_ir.json|…)
_TIMESTAMPED_ARTIFACT = re.compile(
    r"^(.+)_\d{8}_\d{6}(?:_ir)?(?:_html-report|_summary)?\.(?:js|json|html)$"
)


def artifacts_dir() -> Path:
    """Resolve the directory used for generated load-test artifacts.

    Returns:
        Absolute configured artifact directory, or the project default.
    """
    import os

    override = os.getenv("NFE_ARTIFACTS_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_DIR


def k6_app_dir(app_id: str) -> Path:
    """Return ``artifacts/k6/<app>/`` (created lazily)."""
    app = (app_id or "").strip()
    if not app:
        return artifacts_dir()
    ensure_app_dirs(app)
    return artifacts_dir() / app


def _prune_stale_host_artifacts(out_dir: Path, stem: str, keep: Set[str]) -> None:
    """Remove older timestamped artifacts for the same stem.

    Keeps the stable set (``stem.js``, ``stem_ir.json``, report sidecars).
    """
    if not out_dir.is_dir() or not stem:
        return
    prefix = f"{stem}_"
    for path in out_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in keep:
            continue
        if path.name.startswith(prefix) and _TIMESTAMPED_ARTIFACT.match(path.name):
            try:
                path.unlink()
                logger.info("Pruned stale k6 artifact → %s", path.name)
            except OSError:
                pass


def stable_artifact_names(
    target_url: str = "",
    *,
    app: str = "",
    flow: str = "",
    label: str = "",
) -> Dict[str, str]:
    """Return stable filenames for one recorded flow (overwrite on heal).

    Layout: ``k6/<app>/<flow>.js`` and ``k6/<app>/<flow>_ir.json``.

    Args:
        target_url: Journey target URL (used to derive app/flow when omitted).
        app: Explicit app id (URL domain).
        flow: Explicit flow id (Watch-me label / recording stem).
        label: Alternate label used when ``flow`` is empty.

    Returns:
        Mapping with ``script``, ``ir``, ``app``, and ``flow``.
    """
    resolved_app, resolved_flow = resolve_app_and_flow(
        target_url=target_url,
        label=flow or label,
        explicit_app=app,
    )
    if not resolved_app and app:
        resolved_app = slug_flow(app) or app
    if not resolved_flow:
        resolved_flow = "default"
    return {
        "script": f"{resolved_flow}.js",
        "ir": f"{resolved_flow}_ir.json",
        "app": resolved_app,
        "flow": resolved_flow,
    }


def save_k6_script(
    script: str,
    *,
    target_url: str = "",
    filename: Optional[str] = None,
    app: str = "",
    flow: str = "",
    label: str = "",
) -> Dict[str, str]:
    """Write a k6 JavaScript artifact and describe the saved file.

    One recorded flow maps to one stable script path under ``k6/<app>/``.
    Heal loops overwrite the same file instead of creating timestamped copies.

    Args:
        script: Non-empty k6 JavaScript source.
        target_url: Target URL used to derive app/flow when not provided.
        filename: Optional output filename; ``.js`` is appended if absent.
        app: Explicit app id (domain).
        flow: Explicit flow id.
        label: Alternate flow label.

    Returns:
        String-valued metadata containing path fields plus ``app`` / ``flow``.

    Raises:
        NFEValidationError: If ``script`` is empty.
    """
    if not script:
        raise NFEValidationError(
            "Cannot save empty k6 script",
            code=ErrorCode.VALIDATION,
            user_message="Cannot save empty k6 script.",
        )

    names = stable_artifact_names(
        target_url, app=app, flow=flow, label=label
    )
    app_id = names["app"]
    flow_id = names["flow"]
    if app_id:
        out_dir = k6_app_dir(app_id)
    else:
        out_dir = artifacts_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = names["script"]
    filename = safe_artifact_filename(filename, default_suffix=".js")

    path = out_dir / filename
    path = assert_under_jail(path, artifacts_dir())
    path.write_text(script, encoding="utf-8")
    abs_path = str(path.resolve())
    logger.info("Saved k6 script → %s (%s bytes)", abs_path, path.stat().st_size)

    stem = Path(filename).stem
    keep = {
        filename,
        f"{stem}_ir.json",
        "html-report.html",
        "summary.json",
        "k6-points.json",
        f"{stem}_html-report.html",
        f"{stem}_summary.json",
    }
    _prune_stale_host_artifacts(out_dir, stem, keep)

    return {
        "path": abs_path,
        "filename": filename,
        "file_url": path.resolve().as_uri(),
        "size_bytes": str(path.stat().st_size),
        "relative_path": str(path.relative_to(_PROJECT_ROOT))
        if path.is_relative_to(_PROJECT_ROOT)
        else abs_path,
        "app": app_id,
        "flow": flow_id,
    }


def save_load_test_ir(
    ir: Dict[str, Any],
    *,
    target_url: str = "",
    filename: Optional[str] = None,
    app: str = "",
    flow: str = "",
    label: str = "",
) -> Dict[str, str]:
    """Write Load-Test IR as formatted JSON (stable overwrite per app/flow).

    Args:
        ir: Non-empty Load-Test IR mapping.
        target_url: Target URL used to derive app/flow when not provided.
        filename: Optional output filename; ``.json`` is appended if absent.
        app: Explicit app id (domain).
        flow: Explicit flow id.
        label: Alternate flow label.

    Returns:
        String-valued metadata containing the saved path plus ``app`` / ``flow``.

    Raises:
        NFEValidationError: If ``ir`` is empty.
    """
    if not ir:
        raise NFEValidationError(
            "Cannot save empty IR",
            code=ErrorCode.VALIDATION,
            user_message="Cannot save empty IR.",
        )

    names = stable_artifact_names(
        target_url, app=app, flow=flow, label=label
    )
    app_id = names["app"]
    flow_id = names["flow"]
    if app_id:
        out_dir = k6_app_dir(app_id)
    else:
        out_dir = artifacts_dir()
        out_dir.mkdir(parents=True, exist_ok=True)

    if not filename:
        filename = names["ir"]
    filename = safe_artifact_filename(filename, default_suffix=".json")

    path = out_dir / filename
    path = assert_under_jail(path, artifacts_dir())
    path.write_text(json.dumps(ir, indent=2, default=str), encoding="utf-8")
    abs_path = str(path.resolve())
    logger.info("Saved load-test IR → %s", abs_path)

    stem = Path(filename).stem.replace("_ir", "")
    keep = {
        filename,
        f"{stem}.js",
        f"{stem}_html-report.html",
        f"{stem}_summary.json",
        "html-report.html",
        "summary.json",
    }
    _prune_stale_host_artifacts(out_dir, stem, keep)

    return {
        "path": abs_path,
        "filename": filename,
        "file_url": path.resolve().as_uri(),
        "size_bytes": str(path.stat().st_size),
        "relative_path": str(path.relative_to(_PROJECT_ROOT))
        if path.is_relative_to(_PROJECT_ROOT)
        else abs_path,
        "app": app_id,
        "flow": flow_id,
    }


def load_load_test_ir(
    *,
    target_url: str = "",
    app: str = "",
    flow: str = "",
    label: str = "",
) -> Optional[Dict[str, Any]]:
    """Load a previously saved Load-Test IR for an app/flow if present.

    Searches the active artifacts dir (including ``NFE_ARTIFACTS_DIR``), then
    ``artifacts/k6/<app>/`` and ``artifacts/<app>/`` legacy layouts.
    """
    names = stable_artifact_names(
        target_url, app=app, flow=flow, label=label
    )
    app_id = names.get("app") or (app or "").strip()
    flow_id = names.get("flow") or "default"
    ir_name = names.get("ir") or f"{flow_id}_ir.json"

    candidates: List[Path] = []
    if app_id:
        candidates.extend(
            [
                artifacts_dir() / app_id / ir_name,
                k6_app_dir(app_id) / ir_name,
                _PROJECT_ROOT / "artifacts" / "k6" / app_id / ir_name,
                _PROJECT_ROOT / "artifacts" / app_id / ir_name,
            ]
        )
    candidates.append(artifacts_dir() / ir_name)

    seen: Set[str] = set()
    jail_root = artifacts_dir().resolve()
    for path in candidates:
        try:
            key = str(path.resolve())
        except Exception:
            key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            continue
        try:
            jailed = assert_under_jail(path, jail_root)
            data = json.loads(jailed.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load IR %s: %s", path, exc)
            continue
        if isinstance(data, dict) and (data.get("transactions") or data.get("vars") is not None):
            logger.info("Loaded prior Load-Test IR → %s", jailed)
            return data
    return None


def resolve_k6_path(
    *,
    target_url: str = "",
    app: str = "",
    flow: str = "",
    kind: str = "script",
) -> Optional[Path]:
    """Resolve a k6 script or IR path (app-scoped first, then legacy flat)."""
    names = stable_artifact_names(target_url, app=app, flow=flow)
    app_id = names["app"]
    filename = names["script"] if kind == "script" else names["ir"]
    candidates: list[Path] = []
    if app_id:
        candidates.append(artifacts_dir() / app_id / filename)
    # Legacy flat: {host}.js / {host}_ir.json
    if app_id:
        legacy_name = f"{app_id}.js" if kind == "script" else f"{app_id}_ir.json"
        candidates.append(artifacts_dir() / legacy_name)
    candidates.append(artifacts_dir() / filename)
    for candidate in candidates:
        try:
            jailed = assert_under_jail(candidate, artifacts_dir())
        except Exception:
            continue
        if jailed.is_file():
            return jailed
    return None
