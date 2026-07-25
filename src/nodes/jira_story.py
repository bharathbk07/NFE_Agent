"""LangGraph node: process a Jira story from Studio chat."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage

from config.settings import settings
from src.agents.intent_router import ISSUE_KEY_RE, get_latest_human_text
from src.agents.state import AgentState
from src.exceptions import (
    NFEAuthError,
    NFEConfigError,
    NFEError,
    NFESecurityError,
    log_exception,
    node_failure_update,
    to_user_message,
    wrap_unexpected,
)
from src.integrations.jira.client import JiraAPIError, JiraClient, JiraIssue

logger = logging.getLogger("AgentGraph")

FORCE_RE = re.compile(r"\b(force|re[\s-]?run)\b", re.IGNORECASE)
CLARITY_PROCEED_RE = re.compile(
    r"\b("
    r"run\s+(it|again|the\s+(test|smoke|story))|"
    r"go\s+ahead|proceed|continue|yes|"
    r"re[\s-]?run|force"
    r")\b",
    re.IGNORECASE,
)
NFE_COMMENT_RE = re.compile(
    r"("
    r"NFE Agent|"
    r"nfe-(?:agent|recording-ready|blocked|done|queued|running)|"
    r"recording not found|"
    r"Watch-me|"
    r"\bk6\b|"
    r"smoke|"
    r"Test Report|"
    r"target_url"
    r")",
    re.IGNORECASE,
)


def extract_issue_key(text: str) -> Optional[str]:
    """Return the first Jira issue key in ``text``, if any."""
    match = ISSUE_KEY_RE.search(text or "")
    return match.group(1) if match else None


def _wants_force(text: str) -> bool:
    return bool(FORCE_RE.search(text or ""))


def _status_allowed(status: str) -> bool:
    from src.integrations.jira.jql import configured_statuses

    allowed = configured_statuses()
    if not allowed:
        return True
    return (status or "").strip() in allowed


def _list_eligible_issues(client: Optional[JiraClient] = None) -> List[JiraIssue]:
    """Return To Do / In Progress ``nfe-agent`` issues (newest first)."""
    from src.integrations.jira.jql import (
        build_type_fallback_jql,
        effective_poll_jql,
    )
    from src.integrations.jira.labels import LABEL_DONE, LABEL_RUNNING, routing_label

    jira = client or JiraClient()
    jql = effective_poll_jql()
    if "ORDER BY" not in jql.upper():
        jql = f"{jql} ORDER BY created DESC"
    issues = jira.search_jql(jql, max_results=20)
    if issues:
        return issues

    agent = routing_label()
    found: List[JiraIssue] = []
    for issue in jira.search_jql(build_type_fallback_jql(), max_results=30):
        if not issue.has_label(agent):
            continue
        if issue.has_label(LABEL_DONE) or issue.has_label(LABEL_RUNNING):
            continue
        if not _status_allowed(issue.status):
            continue
        found.append(issue)
    return found


def _format_candidate_list(issues: List[JiraIssue]) -> str:
    lines = [
        "Multiple Jira stories match (`nfe-agent`, To Do / In Progress). "
        "Which one should I work on?",
        "",
    ]
    for issue in issues:
        lines.append(
            f"* **{issue.key}** [{issue.status or '?'}] — {issue.summary or '(no summary)'}"
        )
    lines.extend(["", "Reply with e.g. **work on SCRUM-1**."])
    return "\n".join(lines)


def assess_in_progress_comments(
    comments: List[str],
    *,
    description: str = "",
) -> Tuple[bool, str]:
    """Decide whether In Progress comments give enough context to proceed.

    Returns:
        ``(relevant, rationale)``.
    """
    nfe_comments = [c for c in comments if NFE_COMMENT_RE.search(c)]
    if nfe_comments:
        latest = nfe_comments[-1]
        snippet = " ".join(latest.split())[:280]
        return True, f"Latest NFE comment: {snippet}"
    desc = description or ""
    if "target_url" in desc or "```yaml" in desc or "```json" in desc:
        return True, "Story description includes an NFE config block."
    return False, "No relevant NFE comments or config found on this In Progress issue."


def summarize_jira_result(result: Dict[str, Any]) -> str:
    """Build a short chat reply from ``process_issue_key`` output (no secrets)."""
    key = result.get("issue") or "?"
    if result.get("error"):
        return f"Jira **{key}** failed: {result['error']}"
    if result.get("skipped"):
        return (
            f"Skipped **{key}**: {result.get('reason') or 'not eligible'}. "
            "Say **force** or **re-run** if you want to process it anyway."
        )
    if result.get("blocked"):
        reason = result.get("reason") or ""
        errors = result.get("errors") or []
        if reason == "recording_missing" or (
            result.get("blocked") is True and not errors
        ):
            url = result.get("target_url") or ""
            extra = f" ({url})" if url else ""
            return (
                f"**{key}** is blocked: Watch-me recording missing{extra}. "
                "Record the journey, add label `nfe-recording-ready`, then ask again."
            )
        detail = "; ".join(str(e) for e in errors) if errors else (reason or "blocked")
        return f"**{key}** is blocked: {detail}"

    parts = [f"Finished **{key}**."]
    if result.get("ok") is False:
        parts[0] = f"Finished **{key}** with failures."
    if "smoke_ok" in result:
        parts.append(
            "k6 smoke: **pass**." if result.get("smoke_ok") else "k6 smoke: **fail**."
        )
    smoke_summary = (result.get("smoke_summary") or "").strip()
    if smoke_summary:
        parts.append(smoke_summary[:400])
    failed_urls = result.get("failed_urls") or []
    if failed_urls:
        parts.append("Failed requests: " + "; ".join(str(u) for u in failed_urls[:5]))
    paths = []
    for label, field in (
        ("k6", "k6_path"),
        ("IR", "ir_path"),
        ("report", "html_report"),
    ):
        val = result.get(field)
        if val:
            paths.append(f"{label}: `{val}`")
    if paths:
        parts.append("Artifacts: " + "; ".join(paths))
    parts.append("A detailed test report was posted on the Jira issue.")
    return "\n".join(parts)


async def _prepare_and_run(
    key: str,
    *,
    text: str,
    force: bool,
    client: Optional[JiraClient] = None,
) -> Dict[str, Any]:
    """Transition / comment-gate then run ``process_issue_key``."""
    from src.integrations.jira.worker import process_issue_key

    jira = client or JiraClient()
    try:
        issue = jira.get_issue(key)
    except (NFEAuthError, JiraAPIError, NFEError) as exc:
        return node_failure_update(
            exc,
            logger=logger,
            context=f"prepare {key}",
            extra={"jira_issue_key": key},
        )

    status = (issue.status or "").strip()
    in_progress_name = (
        settings.NFE_JIRA_IN_PROGRESS_STATUS or "In Progress"
    ).strip()

    if status.lower() == "to do":
        moved = jira.transition_to_status(key, in_progress_name)
        note = (
            f"Moved **{key}** from To Do → {in_progress_name}."
            if moved
            else f"Could not transition **{key}** to {in_progress_name}; continuing anyway."
        )
        logger.info("%s", note)
        result = await process_issue_key(key, force=True, client=jira)
        reply = note + "\n\n" + summarize_jira_result(result)
        return {
            "jira_issue_key": key,
            "jira_awaiting_clarity": False,
            "jira_candidate_keys": [],
            "messages": [AIMessage(content=reply)],
        }

    if status.lower() == "in progress" or status.lower() == in_progress_name.lower():
        comments = []
        try:
            comments = jira.list_comments(key)
        except JiraAPIError as exc:
            logger.warning("Could not list comments for %s: %s", key, exc)
        relevant, rationale = assess_in_progress_comments(
            comments, description=issue.description or ""
        )
        user_ok = _wants_force(text) or bool(CLARITY_PROCEED_RE.search(text or ""))
        if not relevant and not user_ok:
            return {
                "jira_issue_key": key,
                "jira_awaiting_clarity": True,
                "messages": [
                    AIMessage(
                        content=(
                            f"**{key}** is already *In Progress*, but comments don’t "
                            "give clear NFE instructions.\n\n"
                            f"({rationale})\n\n"
                            "What should I do?\n"
                            "* **re-run** / **force** — run the NFE smoke/load pipeline\n"
                            "* Or describe the next step (e.g. fix recording, change workload)"
                        )
                    )
                ],
            }
        # In Progress with relevant NFE history → resume (force past nfe-done/running)
        result = await process_issue_key(key, force=True, client=jira)
        prefix = ""
        if relevant:
            prefix = f"Using In Progress context for **{key}**: {rationale}\n\n"
        return {
            "jira_issue_key": key,
            "jira_awaiting_clarity": False,
            "jira_candidate_keys": [],
            "messages": [AIMessage(content=prefix + summarize_jira_result(result))],
        }

    # Other statuses (In Review / Done / …) — only if force, or refuse
    if not _status_allowed(status) and not force:
        from src.integrations.jira.jql import configured_statuses

        allowed = ", ".join(configured_statuses() or ["(any)"])
        return {
            "jira_issue_key": key,
            "messages": [
                AIMessage(
                    content=(
                        f"**{key}** is in status *{status or 'unknown'}*. "
                        f"NFE only auto-works stories in: {allowed}. "
                        "Move it to To Do / In Progress, or say **force** to run anyway."
                    )
                )
            ],
        }

    result = await process_issue_key(key, force=force, client=jira)
    return {
        "jira_issue_key": key,
        "jira_awaiting_clarity": False,
        "jira_candidate_keys": [],
        "messages": [AIMessage(content=summarize_jira_result(result))],
    }


async def run_jira_story(state: AgentState) -> Dict[str, Any]:
    """Resolve issue(s), optionally ask which story, then run the REST pipeline."""
    logger.info("Node: run_jira_story starting...")
    text = get_latest_human_text(state.get("messages"))
    # Prefer an explicit key in the latest message over a sticky state key
    key = extract_issue_key(text) or state.get("jira_issue_key")
    force = _wants_force(text)

    try:
        if key:
            return await _prepare_and_run(key, text=text, force=force)

        issues = _list_eligible_issues()
    except NFESecurityError:
        raise
    except (NFEConfigError, NFEAuthError, JiraAPIError, NFEError) as exc:
        return node_failure_update(exc, logger=logger, context="run_jira_story")
    except Exception as exc:
        return node_failure_update(
            wrap_unexpected(exc, user_message="Could not search Jira."),
            logger=logger,
            context="run_jira_story",
        )

    if not issues:
        return {
            "jira_candidate_keys": [],
            "messages": [
                AIMessage(
                    content=(
                        "No matching issues in **To Do** / **In Progress** with label "
                        "`nfe-agent` (and configured issue types). "
                        "Fix the label if it has a trailing comma, or try "
                        "**work on SCRUM-1**."
                    )
                )
            ],
        }

    if len(issues) > 1:
        keys = [i.key for i in issues]
        return {
            "jira_candidate_keys": keys,
            "jira_issue_key": None,
            "messages": [AIMessage(content=_format_candidate_list(issues))],
        }

    only = issues[0]
    return await _prepare_and_run(only.key, text=text, force=force)
