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


def _has_confluence_credentials() -> bool:
    email = (
        settings.CONFLUENCE_EMAIL or settings.JIRA_EMAIL or ""
    ).strip()
    token = (
        settings.CONFLUENCE_API_TOKEN or settings.JIRA_API_TOKEN or ""
    ).strip()
    return bool(email and token)


def _summary_iteration_count(summary: Dict[str, Any]) -> Optional[int]:
    metrics = summary.get("metrics") or {}
    raw = ((metrics.get("iterations") or {}).get("values") or {}).get("count")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def is_watcher_abort(
    smoke_result: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when k6 stopped early because a threshold abortOnFail fired."""
    smoke = smoke_result or {}
    if smoke.get("aborted_by_watcher"):
        return True
    text = " ".join(
        str(smoke.get(k) or "")
        for k in ("summary", "stderr", "stdout")
    ).lower()
    if "aborted by threshold" in text:
        return True
    if "threshold" in text and "abort" in text:
        return True
    _ = summary  # reserved for future summary-based detection
    return False


def _status_code_bucket(code: Any) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        text = str(code or "")
        return "4xx" if text.startswith("4") else ("5xx" if text.startswith("5") else "other")
    if 400 <= c < 500:
        return "4xx"
    if 500 <= c < 600:
        return "5xx"
    if 200 <= c < 400:
        return "2xx"
    return "other"


def is_dominant_4xx_script_failure(
    smoke_result: Optional[Dict[str, Any]] = None,
    summary: Optional[Dict[str, Any]] = None,
) -> bool:
    """True when the run failed mainly due to HTTP 4xx (script/correlation bugs).

    Those runs should **not** publish to Confluence — they pollute the space with
    broken-script noise. Only **passing** smoke/load runs publish.
    """
    smoke = smoke_result or {}
    if smoke.get("ok") is True:
        return False

    status_counts = dict(smoke.get("status_counts") or {})
    four = 0
    total = 0
    for code, count in status_counts.items():
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        total += n
        if _status_code_bucket(code) == "4xx":
            four += n

    failed_urls = [str(u) for u in (smoke.get("failed_urls") or [])]
    url_4xx = sum(
        1
        for u in failed_urls
        if re.search(r"\bstatus=4\d\d\b", u) or "/status=4" in u.lower()
    )

    # Dominant 4xx from status histogram
    if total > 0 and four > 0 and four >= max(3, int(total * 0.25)):
        return True
    # Many labeled 4xx failed URLs even if histogram sparse
    if url_4xx >= 3:
        return True

    # Fail rate high + any 4xx signal and smoke not ok
    metrics = (summary or {}).get("metrics") or {}
    fail_rate = ((metrics.get("http_req_failed") or {}).get("values") or {}).get("rate")
    try:
        fr = float(fail_rate) if fail_rate is not None else 0.0
    except (TypeError, ValueError):
        fr = 0.0
    if smoke.get("ok") is False and fr >= 0.05 and (four > 0 or url_4xx > 0):
        return True
    return False


def explain_confluence_skip(
    smoke_result: Optional[Dict[str, Any]] = None,
    summary_json: str = "",
    *,
    require_config: bool = True,
) -> str:
    """Return a skip reason, or empty string when the run should publish."""
    if require_config:
        if not _truthy_env_publish():
            return "publish_disabled"
        space = (settings.CONFLUENCE_SPACE_KEY or "").strip()
        base = (settings.CONFLUENCE_BASE_URL or settings.JIRA_BASE_URL or "").strip()
        if not space:
            return "no_space_key"
        if not base:
            return "no_base_url"
        if not _has_confluence_credentials():
            return "missing_confluence_credentials"

    smoke = smoke_result or {}
    if smoke.get("skipped"):
        return "smoke_skipped"

    code = smoke.get("code") or ""
    if code in ("K6_SCRIPT_MISSING",):
        return "script_missing"

    # Hard gate: never publish a failed / inconclusive smoke as a "completed" run.
    # SLA fail, script/correlation fail, and unknown status stay on Jira comments only.
    smoke_ok = smoke.get("ok")
    if smoke_ok is not True:
        if smoke_ok is False:
            summary_path = (summary_json or smoke.get("summary_json") or "").strip()
            summary = load_summary(summary_path) if summary_path else {}
            if is_dominant_4xx_script_failure(smoke, summary):
                return "script_4xx_failures"
            if is_watcher_abort(smoke, summary):
                return "sla_watcher_abort_no_publish"
            return "smoke_failed_no_publish"
        return "smoke_unknown_no_publish"

    summary_path = (summary_json or smoke.get("summary_json") or "").strip()
    summary = load_summary(summary_path) if summary_path else {}
    iterations = _summary_iteration_count(summary) if summary else None

    # Proof of completion wins over incidental "timeout" substrings in stderr.
    completed = bool(
        (summary and iterations is not None and iterations > 0)
        or (summary and iterations is None and summary.get("metrics"))
    )

    if completed:
        return ""

    summary_text = str(smoke.get("summary") or "").lower()
    stderr = str(smoke.get("stderr") or "").lower()
    stdout = str(smoke.get("stdout") or "").lower()
    combined = f"{summary_text} {stderr} {stdout}"

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
        if "timeout" in combined or "timed out" in combined:
            return "incomplete_timeout"
        if "spawn failed" in combined:
            return "incomplete_spawn_failed"
        if "script" in combined:
            return "script_missing"
        return "incomplete_infra"

    exit_code = smoke.get("exit_code")
    if exit_code is None and not smoke and not summary_json:
        return "incomplete_no_summary"
    if exit_code == -1:
        return "incomplete_no_summary"
    if summary and iterations is not None and iterations <= 0:
        return "incomplete_no_summary"

    if exit_code is None:
        return "incomplete_no_summary"
    try:
        ec = int(exit_code)
    except (TypeError, ValueError):
        return "incomplete_no_summary"
    if ec < 0:
        return "incomplete_no_summary"

    if "aborted" in combined or "interrupted" in combined:
        if not summary:
            return "incomplete_aborted"

    return ""


def should_publish_to_confluence(
    smoke_result: Optional[Dict[str, Any]] = None,
    summary_json: str = "",
    *,
    require_config: bool = True,
) -> bool:
    """Return True only for **passing** completed k6 runs.

    Failed smoke/SLA, mid-run abort, timeout, skipped, or missing script → False.
    Failures stay on the Jira story comment; Confluence is evidence of green runs.
    """
    return not bool(
        explain_confluence_skip(
            smoke_result, summary_json, require_config=require_config
        )
    )


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
        msg = str(exc)
        lower = msg.lower()
        is_auth = any(
            t in lower
            for t in ("auth", "401", "403", "credential", "api token", "email")
        )
        if is_auth:
            logger.error("Confluence publish soft-failed (auth/config): %s", exc)
        else:
            logger.error("Confluence publish soft-failed: %s", exc)
        return {
            "published": False,
            "skipped_reason": f"auth_or_api_error: {exc}" if is_auth else f"publish error: {exc}",
            "error": msg,
        }


def publish_run_results(result: Dict[str, Any]) -> Dict[str, Any]:
    """Publish a completed run under the fixed parent hierarchy.

    Returns a dict with ``published``, URLs, and optional ``skipped_reason``.
    """
    smoke = result.get("smoke_result") or result.get("k6_smoke") or {}
    summary_json = str(
        result.get("summary_json") or smoke.get("summary_json") or ""
    )
    skip_reason = explain_confluence_skip(smoke, summary_json)
    if skip_reason:
        logger.info("Skipping Confluence publish: %s", skip_reason)
        return {"published": False, "skipped_reason": skip_reason}

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
    watcher_stop = bool(
        result.get("aborted_by_watcher")
        or smoke.get("aborted_by_watcher")
        or is_watcher_abort(smoke, summary)
    )
    smoke_ok = (
        result.get("smoke_ok") if "smoke_ok" in result else smoke.get("ok")
    )
    status_counts = dict(
        result.get("status_counts") or smoke.get("status_counts") or {}
    )
    status = resolve_run_status(
        smoke_ok=smoke_ok,
        summary=summary,
        aborted_by_watcher=watcher_stop,
        status_counts=status_counts,
    )
    workload = result.get("workload") if isinstance(result.get("workload"), dict) else {}
    workload_source = str(result.get("workload_source") or "")

    k6_path = str(result.get("k6_path") or "")
    html_path = str(result.get("html_report") or smoke.get("html_report") or "")
    ir_path = str(result.get("ir_path") or "")
    points_json = str(
        result.get("points_json")
        or smoke.get("points_json")
        or ""
    )
    if not points_json and k6_path:
        candidate = Path(k6_path).with_name("k6-points.json")
        if candidate.is_file():
            points_json = str(candidate)

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
        points_json=points_json,
        transactions=list(result.get("transactions") or []),
        heal_notes=list(result.get("heal_notes") or smoke.get("heal_notes") or []),
        failed_checks=list(
            result.get("failed_checks") or smoke.get("failed_checks") or []
        ),
        failed_urls=list(result.get("failed_urls") or smoke.get("failed_urls") or []),
        status_counts=status_counts,
        exit_code=result.get("exit_code", smoke.get("exit_code")),
        attachment_names=planned_names,
        workload=workload,
        workload_source=workload_source,
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
            points_json=points_json,
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
            status_counts=status_counts,
            exit_code=result.get("exit_code", smoke.get("exit_code")),
            attachment_names=attach_names,
            workload=workload,
            workload_source=workload_source,
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
