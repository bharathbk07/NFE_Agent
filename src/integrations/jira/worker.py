"""Orchestrate Jira issue pickup → recording gate → NFE perf run → comments."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from src.integrations.jira import comments
from src.integrations.jira.client import JiraAPIError, JiraClient, JiraIssue
from src.integrations.jira.labels import (
    LABEL_BLOCKED,
    LABEL_DONE,
    LABEL_QUEUED,
    LABEL_RECORDING_READY,
    LABEL_RUNNING,
    routing_label,
)
from src.integrations.jira.pipeline import run_perf_for_request
from src.integrations.jira.story_parser import parse_story_text
from src.exceptions import (
    ErrorCode,
    NFEAuthError,
    NFEConfigError,
    NFEError,
    NFESecurityError,
    log_exception,
    to_user_message,
    wrap_unexpected,
)
from src.utils.recording_store import resolve_recording_path

logger = logging.getLogger(__name__)


def should_process(issue: JiraIssue, *, force: bool = False) -> tuple[bool, str]:
    """Return whether the worker should process this issue and a reason."""
    from src.integrations.jira.jql import configured_issue_types, configured_statuses

    agent = routing_label()
    if not issue.has_label(agent):
        return False, f"missing routing label {agent}"
    allowed_types = configured_issue_types()
    if allowed_types:
        itype = (issue.issue_type or "").strip()
        if itype and itype not in allowed_types:
            return False, f"issuetype {itype!r} not in NFE_JIRA_ISSUE_TYPES"
    allowed_statuses = configured_statuses()
    if allowed_statuses:
        status = (issue.status or "").strip()
        if status and status not in allowed_statuses:
            return False, f"status {status!r} not in NFE_JIRA_STATUSES"
    if issue.has_label(LABEL_RUNNING) and not force:
        return False, "already running"
    if issue.has_label(LABEL_DONE) and not force:
        # Allow resume when recording-ready was added after a blocked run
        if issue.has_label(LABEL_RECORDING_READY):
            return True, "recording-ready after done"
        return False, "already done"
    return True, "ok"


async def process_issue_key(
    key: str,
    *,
    force: bool = False,
    client: Optional[JiraClient] = None,
) -> Dict[str, Any]:
    """Full worker pipeline for one issue key."""
    try:
        jira = client or JiraClient()
    except (NFEConfigError, ValueError) as exc:
        log_exception(logger, exc if isinstance(exc, NFEError) else wrap_unexpected(exc), context="jira_client")
        return {"ok": False, "error": to_user_message(exc), "issue": key, "code": ErrorCode.CONFIG_MISSING}

    try:
        issue = jira.get_issue(key)
    except NFEAuthError as exc:
        log_exception(logger, exc, context=f"get_issue {key}")
        return {
            "ok": False,
            "error": to_user_message(exc),
            "status_code": (exc.details or {}).get("status_code"),
            "issue": key,
            "code": exc.code,
        }
    except JiraAPIError as exc:
        log_exception(logger, exc, context=f"get_issue {key}")
        return {
            "ok": False,
            "error": to_user_message(exc),
            "status_code": exc.status_code,
            "issue": key,
            "code": exc.code,
        }
    except httpx.HTTPError as exc:
        wrapped = wrap_unexpected(
            exc,
            code=ErrorCode.JIRA_API,
            user_message=f"Jira HTTP error for {key}.",
        )
        log_exception(logger, wrapped, context=f"get_issue {key}")
        return {"ok": False, "error": to_user_message(wrapped), "issue": key, "code": wrapped.code}

    ok, reason = should_process(issue, force=force)
    if not ok:
        logger.info("Skip %s: %s", key, reason)
        return {"skipped": True, "reason": reason, "issue": key}

    try:
        jira.set_lifecycle(
            key,
            add=[LABEL_QUEUED, LABEL_RUNNING],
            remove=[LABEL_BLOCKED],
        )
        try:
            jira.add_comment(key, comments.comment_queued(key))
        except JiraAPIError as exc:
            logger.warning("Failed to comment queued on %s: %s", key, exc)

        req = parse_story_text(
            summary=issue.summary,
            description=issue.description,
            acceptance_criteria=issue.acceptance_criteria,
        )

        rec_path = resolve_recording_path(
            req.recording_hint or req.host_hint or req.target_url
        )
        # Fill target_url from Watch-me recording when story only names a recording
        if not req.target_url and rec_path is not None:
            try:
                from src.utils.recording_store import load_watch_me_recording

                loaded = load_watch_me_recording(rec_path)
                req.target_url = str(loaded.get("target_url") or "").strip()
                if req.target_url:
                    req.errors = [
                        e
                        for e in req.errors
                        if "target_url" not in e.lower()
                    ]
                    logger.info(
                        "Filled target_url from recording %s → %s",
                        rec_path.name,
                        req.target_url,
                    )
            except Exception as exc:
                logger.warning("Could not load recording for URL: %s", exc)

        if req.errors and not req.target_url and not req.recording_hint:
            msg = comments.comment_blocked("\n".join(f"* {e}" for e in req.errors))
            jira.add_comment(key, msg)
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
            return {"ok": False, "blocked": True, "errors": req.errors, "issue": key}

        if req.errors and req.target_url:
            # Soft policy / parse warnings with a URL still present → block
            msg = comments.comment_blocked("\n".join(f"* {e}" for e in req.errors))
            jira.add_comment(key, msg)
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
            return {"ok": False, "blocked": True, "errors": req.errors, "issue": key}

        if rec_path is None:
            rec_path = resolve_recording_path(
                req.recording_hint or req.host_hint or req.target_url
            )
        if rec_path is None:
            jira.add_comment(
                key,
                comments.comment_missing_recording(
                    target_url=req.target_url,
                    recording_hint=req.recording_hint,
                    host_hint=req.host_hint,
                ),
            )
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
            return {
                "ok": False,
                "blocked": True,
                "reason": "recording_missing",
                "issue": key,
                "target_url": req.target_url,
            }

        if not req.target_url:
            msg = comments.comment_blocked(
                "* No target_url in story and recording has no target_url."
            )
            jira.add_comment(key, msg)
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
            return {
                "ok": False,
                "blocked": True,
                "errors": ["missing target_url"],
                "issue": key,
            }

        result = await run_perf_for_request(req, recording_path=rec_path)
        if result.get("blocked"):
            jira.add_comment(
                key,
                comments.comment_missing_recording(
                    target_url=req.target_url,
                    recording_hint=req.recording_hint,
                    host_hint=req.host_hint,
                ),
            )
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
            return {**result, "issue": key}

        jira.add_comment(
            key,
            comments.comment_results(
                issue_key=key,
                target_url=result.get("target_url") or req.target_url,
                smoke_ok=result.get("smoke_ok"),
                smoke_summary=result.get("smoke_summary") or "",
                workload=req.workload,
                k6_path=str(result.get("k6_path") or ""),
                ir_path=str(result.get("ir_path") or ""),
                html_report=str(result.get("html_report") or ""),
                transactions=result.get("transactions") or [],
                heal_notes=result.get("heal_notes") or [],
                story_summary=issue.summary,
                recording_path=str(result.get("recording_path") or ""),
                failed_checks=result.get("failed_checks") or [],
                failed_urls=result.get("failed_urls") or [],
                status_counts=result.get("status_counts") or {},
                summary_json=str(result.get("summary_json") or ""),
                exit_code=result.get("exit_code"),
            ),
        )
        jira.set_lifecycle(
            key,
            add=[LABEL_DONE],
            remove=[
                LABEL_RUNNING,
                LABEL_QUEUED,
                LABEL_BLOCKED,
                LABEL_RECORDING_READY,
            ],
        )
        return {**result, "issue": key, "skipped": False}
    except NFESecurityError:
        raise
    except (NFEAuthError, JiraAPIError) as exc:
        log_exception(logger, exc, context=f"process {key}")
        try:
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
        except Exception:
            pass
        return {
            "ok": False,
            "error": to_user_message(exc),
            "status_code": getattr(exc, "status_code", None)
            or (getattr(exc, "details", None) or {}).get("status_code"),
            "issue": key,
            "code": getattr(exc, "code", ErrorCode.JIRA_API),
        }
    except NFEError as exc:
        log_exception(logger, exc, context=f"process {key}")
        try:
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
        except Exception:
            pass
        return {"ok": False, "error": to_user_message(exc), "issue": key, "code": exc.code}
    except Exception as exc:
        wrapped = wrap_unexpected(exc, user_message=f"Unexpected failure processing {key}.")
        log_exception(logger, wrapped, context=f"process {key}")
        try:
            jira.set_lifecycle(
                key,
                add=[LABEL_BLOCKED],
                remove=[LABEL_RUNNING, LABEL_QUEUED],
            )
        except Exception:
            pass
        return {"ok": False, "error": to_user_message(wrapped), "issue": key, "code": wrapped.code}


async def poll_once(
    *,
    jql: Optional[str] = None,
    client: Optional[JiraClient] = None,
    force: bool = False,
) -> list[Dict[str, Any]]:
    """Search JQL and process matching issues sequentially."""
    jira = client or JiraClient()
    from src.integrations.jira.jql import effective_poll_jql

    query = jql or effective_poll_jql()
    issues = jira.search_jql(query)
    results = []
    for issue in issues:
        # Hydrate full fields
        results.append(await process_issue_key(issue.key, force=force, client=jira))
    return results
