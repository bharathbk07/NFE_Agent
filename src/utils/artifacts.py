"""Persist generated artifacts (k6 scripts, IR) to disk for download."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set
from urllib.parse import urlparse

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


def _slug_host(target_url: str) -> str:
    """Convert a target URL host into a filesystem-safe stem.

    Args:
        target_url: Absolute or partial target URL.

    Returns:
        Sanitized host stem limited to 60 characters.
    """
    try:
        host = urlparse(target_url or "").netloc or "script"
    except Exception:
        host = "script"
    host = re.sub(r"[^a-zA-Z0-9._-]+", "_", host).strip("._") or "script"
    return host[:60]


def stable_artifact_names(target_url: str) -> Dict[str, str]:
    """Return stable filenames for one recorded flow (overwrite on heal).

    Args:
        target_url: Journey target URL.

    Returns:
        Mapping with ``script`` and ``ir`` filenames (no timestamps).
    """
    host = _slug_host(target_url)
    return {
        "script": f"{host}.js",
        "ir": f"{host}_ir.json",
    }


def _prune_stale_host_artifacts(out_dir: Path, host: str, keep: Set[str]) -> None:
    """Remove older timestamped artifacts for the same host.

    Keeps the stable set (``host.js``, ``host_ir.json``, report sidecars).
    """
    if not out_dir.is_dir() or not host:
        return
    prefix = f"{host}_"
    for path in out_dir.iterdir():
        if not path.is_file():
            continue
        if path.name in keep:
            continue
        # Timestamped legacy: host_20260721_191219.js / _ir.json / reports
        if path.name.startswith(prefix) and _TIMESTAMPED_ARTIFACT.match(path.name):
            try:
                path.unlink()
                logger.info("Pruned stale k6 artifact → %s", path.name)
            except OSError:
                pass


def save_k6_script(
    script: str,
    *,
    target_url: str = "",
    filename: Optional[str] = None,
) -> Dict[str, str]:
    """Write a k6 JavaScript artifact and describe the saved file.

    One recorded flow maps to one stable script path (``{host}.js``). Heal
    loops overwrite the same file instead of creating timestamped copies.

    Args:
        script: Non-empty k6 JavaScript source.
        target_url: Target URL used to derive a default filename.
        filename: Optional output filename; ``.js`` is appended if absent.

    Returns:
        String-valued metadata containing ``path``, ``filename``, ``file_url``,
        ``size_bytes``, and ``relative_path``.

    Raises:
        ValueError: If ``script`` is empty.
    """
    if not script:
        raise ValueError("Cannot save empty k6 script")

    out_dir = artifacts_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    host = _slug_host(target_url)
    if not filename:
        filename = stable_artifact_names(target_url)["script"]
    if not filename.endswith(".js"):
        filename = f"{filename}.js"

    path = out_dir / filename
    path.write_text(script, encoding="utf-8")
    abs_path = str(path.resolve())
    logger.info("Saved k6 script → %s (%s bytes)", abs_path, path.stat().st_size)

    keep = {
        filename,
        f"{host}_ir.json",
        "html-report.html",
        "summary.json",
        "k6-points.json",
        f"{Path(filename).stem}_html-report.html",
        f"{Path(filename).stem}_summary.json",
    }
    _prune_stale_host_artifacts(out_dir, host, keep)

    return {
        "path": abs_path,
        "filename": filename,
        "file_url": path.resolve().as_uri(),
        "size_bytes": str(path.stat().st_size),
        "relative_path": str(path.relative_to(_PROJECT_ROOT))
        if path.is_relative_to(_PROJECT_ROOT)
        else abs_path,
    }


def save_load_test_ir(
    ir: Dict[str, Any],
    *,
    target_url: str = "",
    filename: Optional[str] = None,
) -> Dict[str, str]:
    """Write Load-Test IR as formatted JSON (stable overwrite per host).

    Args:
        ir: Non-empty Load-Test IR mapping.
        target_url: Target URL used to derive a default filename.
        filename: Optional output filename; ``.json`` is appended if absent.

    Returns:
        String-valued metadata containing the saved path, name, URL, size, and
        project-relative path when available.

    Raises:
        ValueError: If ``ir`` is empty.
    """
    if not ir:
        raise ValueError("Cannot save empty IR")

    out_dir = artifacts_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    host = _slug_host(target_url)
    if not filename:
        filename = stable_artifact_names(target_url)["ir"]
    if not filename.endswith(".json"):
        filename = f"{filename}.json"

    path = out_dir / filename
    path.write_text(json.dumps(ir, indent=2, default=str), encoding="utf-8")
    abs_path = str(path.resolve())
    logger.info("Saved load-test IR → %s", abs_path)

    keep = {
        filename,
        f"{host}.js",
        f"{host}_html-report.html",
        f"{host}_summary.json",
    }
    _prune_stale_host_artifacts(out_dir, host, keep)

    return {
        "path": abs_path,
        "filename": filename,
        "file_url": path.resolve().as_uri(),
        "size_bytes": str(path.stat().st_size),
        "relative_path": str(path.relative_to(_PROJECT_ROOT))
        if path.is_relative_to(_PROJECT_ROOT)
        else abs_path,
    }
