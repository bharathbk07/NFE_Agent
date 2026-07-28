"""Orchestrate Confluence publishing for completed NFE k6 runs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from config.settings import settings
from src.integrations.confluence.client import ConfluenceClient
from src.integrations.confluence.pages import find_or_create_page, update_page_body
from src.integrations.confluence.report import (
    build_flow_latest_storage_body,
    build_placeholder_body,
    build_run_storage_body,
    load_summary,
    resolve_run_status,
    threshold_rows,
)
from src.integrations.confluence.security import sanitize_title

logger = logging.getLogger(__name__)


def _truthy_env_publish() -> bool:
    return bool(settings.NFE_CONFLUENCE_PUBLISH)


def _configured() -> bool:
    space = (settings.CONFLUENCE_SPACE_KEY or "").strip()
    base = (settings.CONFLUENCE_BASE_URL or settings.JIRA_BASE_URL or "").strip()
    return bool(space and base and _truthy_env_publish())


def should_publish_to_confluence(
    smoke_result: Optional[Dict[str, Any]] = None,
    summary_json: str = "",
    *,
    require_config: bool = True,
) -> bool:
    """Return True only for fully completed k6 runs (SLA pass/fail/none).

    Mid-run abort / timeout / skipped / missing script → False.
    """
    if require_config and not _configured():
        return False

    smoke = smoke_result or {}
    if smoke.get("skipped"):
        return False

    summary_text = str(smoke.get("summary") or "").lower()
    stderr = str(smoke.get("stderr") or "").lower()
    combined = f"{summary_text} {stderr}"

    # Explicit incomplete / infrastructure failures
    if any(
        token in combined
        for token in (
            "timeout",
            "timed out",
            "spawn failed",
            "script missing",
            "script not found",
            "k6 missing",
            "k6 not found",
        )
    ):
        return False

    code = smoke.get("code") or ""
    if code in ("K6_SCRIPT_MISSING",):
        return False

    exit_code = smoke.get("exit_code")
    # Never ran
    if exit_code is None and not smoke and not summary_json:
        return False
    if exit_code == -1:
        return False

    summary_path = (summary_json or smoke.get("summary_json") or "").strip()
    summary = load_summary(summary_path) if summary_path else {}

    # Prefer summary.json as proof the handleSummary path ran (full completion)
    if summary:
        metrics = summary.get("metrics") or {}
        iterations = ((metrics.get("iterations") or {}).get("values") or {}).get("count")
        # Abort mid-run often yields 0 iterations with a short/empty summary state
        if iterations is not None:
            try:
                if int(iterations) <= 0:
                    return False
            except (TypeError, ValueError):
                pass
        return True

    # No summary file: only publish if k6 exited cleanly-ish (0 or threshold fail 99)
    # Threshold failures still complete the run; k6 often exits non-zero.
    if exit_code is None:
        return False
    try:
        ec = int(exit_code)
    except (TypeError, ValueError):
        return False
    # -1 already handled; spawn/timeout paths set -1
    if ec < 0:
        return False
    # Completed run without summary.json (unusual) — allow if stdout looks finished
    stdout = str(smoke.get("stdout") or "").lower()
    if "running" in stdout and "complete" not in stdout and "checks" not in stdout:
        # Heuristic: aborted early
        if "aborted" in combined or "interrupted" in combined:
            return False
    if "aborted" in combined or "interrupted" in combined:
        return False
    return True


def resolve_flow_name(
    *,
    recording_file: str = "",
    recording_hint: str = "",
    target_url: str = "",
) -> str:
    """Watch-me / recording stem, else hint, else host slug."""
    if recording_file:
        stem = Path(recording_file).stem.strip()
        if stem:
            return sanitize_title(stem)
    if recording_hint and recording_hint.strip():
        return sanitize_title(recording_hint.strip())
    if target_url:
        host = urlparse(target_url).netloc or target_url
        host = re.sub(r"[^\w.\-]+", "-", host).strip("-")
        return sanitize_title(host or "unknown-host")
    return "unknown-flow"


def _attachment_basename(flow_name: str, suffix: str, timestamp: str) -> str:
    slug = re.sub(r"[^\w.\-]+", "-", flow_name).strip("-")[:40] or "flow"
    ts = re.sub(r"[^\d]", "", timestamp)[:12] or "run"
    return f"{slug}-{suffix}-{ts}"


def try_publish_run_results(result: Dict[str, Any]) -> Dict[str, Any]:
    """Call :func:`publish_run_results` and soft-fail on errors."""
    try:
        return publish_run_results(result)
    except Exception as exc:
        logger.warning("Confluence publish soft-failed: %s", exc)
        return {
            "published": False,
            "skipped_reason": f"publish error: {exc}",
            "error": str(exc),
        }


def publish_run_results(result: Dict[str, Any]) -> Dict[str, Any]:
    """Publish a completed run under the fixed parent hierarchy.

    Returns a dict with ``published``, URLs, and optional ``skipped_reason``.
    """
    smoke = result.get("smoke_result") or result.get("k6_smoke") or {}
    summary_json = str(
        result.get("summary_json") or smoke.get("summary_json") or ""
    )
    if not should_publish_to_confluence(smoke, summary_json):
        reason = "run incomplete, skipped, or Confluence not configured"
        logger.info("Skipping Confluence publish: %s", reason)
        return {"published": False, "skipped_reason": reason}

    flow_name = resolve_flow_name(
        recording_file=str(
            result.get("recording_file") or result.get("recording_path") or ""
        ),
        recording_hint=str(result.get("recording_hint") or ""),
        target_url=str(result.get("target_url") or ""),
    )
    parent_title = sanitize_title(
        settings.CONFLUENCE_PARENT_TITLE or "Performance Testing and Engineering"
    )
    now = datetime.now(timezone.utc).astimezone()
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    run_title = sanitize_title(f"Run {timestamp}")

    summary = load_summary(summary_json)
    status = resolve_run_status(
        smoke_ok=result.get("smoke_ok")
        if "smoke_ok" in result
        else smoke.get("ok"),
        summary=summary,
    )

    client = ConfluenceClient()
    root_body = build_placeholder_body(parent_title)
    root, _ = find_or_create_page(
        client,
        title=parent_title,
        parent_id=None,
        storage_body=root_body,
    )
    root_id = str(root.get("id") or "")

    flow_body = build_placeholder_body(flow_name)
    flow, _ = find_or_create_page(
        client,
        title=flow_name,
        parent_id=root_id,
        storage_body=flow_body,
    )
    flow_id = str(flow.get("id") or "")

    attach_names: List[str] = []
    k6_path = str(result.get("k6_path") or "")
    html_path = str(result.get("html_report") or smoke.get("html_report") or "")
    ir_path = str(result.get("ir_path") or "")

    planned_names: List[str] = []
    upload_specs: List[tuple[str, str]] = []
    if k6_path and Path(k6_path).is_file():
        name = _attachment_basename(flow_name, "k6", timestamp) + ".js"
        planned_names.append(name)
        upload_specs.append((k6_path, name))
    if html_path and Path(html_path).is_file():
        name = _attachment_basename(flow_name, "html-report", timestamp) + ".html"
        planned_names.append(name)
        upload_specs.append((html_path, name))
    if ir_path and Path(ir_path).is_file():
        name = _attachment_basename(flow_name, "ir", timestamp) + ".json"
        planned_names.append(name)
        upload_specs.append((ir_path, name))

    run_body = build_run_storage_body(
        status=status,
        flow_name=flow_name,
        target_url=str(result.get("target_url") or ""),
        jira_issue_key=str(result.get("jira_issue_key") or ""),
        timestamp=timestamp,
        smoke_summary=str(result.get("smoke_summary") or smoke.get("summary") or ""),
        summary_json=summary_json,
        transactions=list(result.get("transactions") or []),
        heal_notes=list(result.get("heal_notes") or smoke.get("heal_notes") or []),
        failed_checks=list(
            result.get("failed_checks") or smoke.get("failed_checks") or []
        ),
        failed_urls=list(result.get("failed_urls") or smoke.get("failed_urls") or []),
        status_counts=dict(
            result.get("status_counts") or smoke.get("status_counts") or {}
        ),
        exit_code=result.get("exit_code", smoke.get("exit_code")),
        attachment_names=planned_names,
    )
    run_page, _ = find_or_create_page(
        client,
        title=run_title,
        parent_id=flow_id,
        storage_body=run_body,
    )
    run_id = str(run_page.get("id") or "")
    update_page_body(client, run_page, title=run_title, storage_body=run_body)

    for path, name in upload_specs:
        try:
            client.upload_attachment(page_id=run_id, file_path=path, filename=name)
            attach_names.append(name)
        except Exception as exc:
            logger.warning("Confluence attachment upload failed (%s): %s", name, exc)

    if attach_names:
        run_body = build_run_storage_body(
            status=status,
            flow_name=flow_name,
            target_url=str(result.get("target_url") or ""),
            jira_issue_key=str(result.get("jira_issue_key") or ""),
            timestamp=timestamp,
            smoke_summary=str(
                result.get("smoke_summary") or smoke.get("summary") or ""
            ),
            summary_json=summary_json,
            transactions=list(result.get("transactions") or []),
            heal_notes=list(
                result.get("heal_notes") or smoke.get("heal_notes") or []
            ),
            failed_checks=list(
                result.get("failed_checks") or smoke.get("failed_checks") or []
            ),
            failed_urls=list(
                result.get("failed_urls") or smoke.get("failed_urls") or []
            ),
            status_counts=dict(
                result.get("status_counts") or smoke.get("status_counts") or {}
            ),
            exit_code=result.get("exit_code", smoke.get("exit_code")),
            attachment_names=attach_names,
        )
        fresh = client.get_page(run_id, expand="version")
        update_page_body(client, fresh, title=run_title, storage_body=run_body)

    run_url = client.page_url(run_id)
    flow_url = client.page_url(flow_id)
    parent_url = client.page_url(root_id)

    latest_body = build_flow_latest_storage_body(
        flow_name=flow_name,
        status=status,
        run_title=run_title,
        run_url=run_url,
        target_url=str(result.get("target_url") or ""),
        timestamp=timestamp,
        jira_issue_key=str(result.get("jira_issue_key") or ""),
    )
    fresh_flow = client.get_page(flow_id, expand="version")
    update_page_body(client, fresh_flow, title=flow_name, storage_body=latest_body)

    thr = threshold_rows(summary)
    logger.info(
        "Published Confluence run page %s (flow=%s, status=%s, sla_rules=%d)",
        run_url,
        flow_name,
        status,
        len(thr),
    )
    return {
        "published": True,
        "status": status,
        "flow_name": flow_name,
        "run_title": run_title,
        "parent_url": parent_url,
        "flow_url": flow_url,
        "run_url": run_url,
        "attachments": attach_names,
        "skipped_reason": "",
    }
