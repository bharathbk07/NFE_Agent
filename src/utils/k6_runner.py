"""Run generated k6 scripts as a local smoke (1 VU / 2 iterations)."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def k6_available() -> bool:
    """Return True when the ``k6`` binary is on PATH."""
    return shutil.which("k6") is not None


def run_k6_smoke(
    script_path: str,
    *,
    vus: int = 1,
    iterations: int = 2,
    timeout_s: int = 120,
) -> Dict[str, Any]:
    """Execute a k6 smoke run and parse basic failure signals.

    Args:
        script_path: Absolute or relative path to a ``.js`` k6 script.
        vus: Virtual users (default 1).
        iterations: Shared iterations (default 2).
        timeout_s: Subprocess timeout in seconds.

    Returns:
        Dictionary with ``ok``, ``skipped``, ``exit_code``, ``stdout``,
        ``stderr``, ``failed_checks``, ``failed_urls``, ``summary``, and
        optional ``html_report`` / ``summary_json`` paths.
    """
    path = Path(script_path)
    if not path.is_file():
        return {
            "ok": False,
            "skipped": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Script not found: {script_path}",
            "failed_checks": [],
            "failed_urls": [],
            "summary": "script missing",
        }

    if not k6_available():
        return {
            "ok": False,
            "skipped": True,
            "exit_code": -1,
            "stdout": "",
            "stderr": "k6 not found on PATH — install k6 to enable smoke validation",
            "failed_checks": [],
            "failed_urls": [],
            "summary": "k6 missing",
        }

    from src.utils.k6_html_report import find_html_report, report_paths_for_script
    from src.utils.k6_report_builder import write_html_report

    report_paths = report_paths_for_script(path)
    points_path = str(path.resolve().with_name("k6-points.json"))
    env = os.environ.copy()
    env["NFE_K6_HTML_REPORT"] = report_paths["html"]
    env["NFE_K6_SUMMARY_JSON"] = report_paths["json"]

    cmd = [
        "k6",
        "run",
        "--out",
        f"json={points_path}",
        str(path.resolve()),
    ]
    # Script options already define smoke (1 VU × 2 iterations). Avoid CLI
    # overrides that fight scenario blocks.
    logger.info("Running k6 smoke: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
            cwd=str(path.parent),
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else "k6 timed out"
        return {
            "ok": False,
            "skipped": False,
            "exit_code": -1,
            "stdout": out,
            "stderr": err,
            "failed_checks": [],
            "failed_urls": [],
            "summary": "timeout",
            "html_report": find_html_report(path) or "",
            "summary_json": report_paths["json"] if Path(report_paths["json"]).is_file() else "",
        }
    except OSError as exc:
        return {
            "ok": False,
            "skipped": False,
            "exit_code": -1,
            "stdout": "",
            "stderr": str(exc),
            "failed_checks": [],
            "failed_urls": [],
            "summary": "spawn failed",
        }

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = stdout + "\n" + stderr
    failed_checks = _parse_failed_checks(combined)
    failed_urls = _parse_failed_urls(combined)
    ok = proc.returncode == 0
    summary = "passed" if ok else f"failed (exit {proc.returncode})"

    html_report = ""
    try:
        html_report = write_html_report(
            script_path=path,
            points_path=points_path,
            summary_path=report_paths["json"],
            html_path=report_paths["html"],
        )
    except Exception as exc:
        logger.warning("Failed to build HTML report from points: %s", exc)
        html_report = find_html_report(path) or ""

    summary_json = (
        report_paths["json"] if Path(report_paths["json"]).is_file() else ""
    )
    if html_report:
        logger.info("k6 HTML report → %s", html_report)
    return {
        "ok": ok,
        "skipped": False,
        "exit_code": proc.returncode,
        "stdout": stdout[-8000:],
        "stderr": stderr[-4000:],
        "failed_checks": failed_checks[:40],
        "failed_urls": failed_urls[:40],
        "summary": summary,
        "html_report": html_report,
        "summary_json": summary_json,
    }


def _parse_failed_checks(text: str) -> List[str]:
    """Extract check names that reported failures from k6 output."""
    failed: List[str] = []
    for m in re.finditer(r"✗\s+([^\n]+)", text):
        name = m.group(1).strip()
        # Skip threshold / banner noise
        if "rate=" in name or "duration" in name.lower() or name.startswith("VUs"):
            continue
        if name and name not in failed:
            failed.append(name)
    return failed


def _parse_failed_urls(text: str) -> List[str]:
    """Best-effort extraction of URLs mentioned near HTTP errors."""
    urls: List[str] = []
    for m in re.finditer(r"https?://[^\s\"']+", text):
        u = m.group(0).rstrip(".,);]")
        if u not in urls:
            urls.append(u)
    return urls
