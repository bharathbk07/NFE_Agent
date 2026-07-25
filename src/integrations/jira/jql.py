"""JQL helpers for NFE Jira pickup (labels + issue types + statuses)."""

from __future__ import annotations

from typing import List

from config.settings import settings
from src.integrations.jira.labels import (
    LABEL_DONE,
    LABEL_RUNNING,
    routing_label,
)


def configured_issue_types() -> List[str]:
    """Parse ``NFE_JIRA_ISSUE_TYPES`` (comma-separated). Empty / ``*`` = any type."""
    raw = (settings.NFE_JIRA_ISSUE_TYPES or "").strip()
    if not raw or raw == "*":
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def configured_statuses() -> List[str]:
    """Parse ``NFE_JIRA_STATUSES`` (comma-separated). Empty / ``*`` = any status."""
    raw = (settings.NFE_JIRA_STATUSES or "").strip()
    if not raw or raw == "*":
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _quote_list(values: List[str]) -> str:
    return ", ".join(f'"{v}"' for v in values)


def build_default_poll_jql() -> str:
    """Default JQL: routing label, board statuses, not done/running labels, types."""
    label = routing_label()
    parts = [
        f'labels = "{label}"',
        f'labels != "{LABEL_DONE}"',
        f'labels != "{LABEL_RUNNING}"',
    ]
    statuses = configured_statuses()
    if statuses:
        parts.append(f"status in ({_quote_list(statuses)})")
    types = configured_issue_types()
    if types:
        parts.append(f"issuetype in ({_quote_list(types)})")
    return " AND ".join(parts)


def effective_poll_jql() -> str:
    """Use ``NFE_JIRA_POLL_JQL`` when set; otherwise build from label + types + statuses."""
    custom = (settings.NFE_JIRA_POLL_JQL or "").strip()
    if custom:
        return custom
    return build_default_poll_jql()


def build_type_fallback_jql() -> str:
    """Broader JQL used when exact label search misses (e.g. trailing-comma labels)."""
    parts: List[str] = []
    types = configured_issue_types()
    if types:
        parts.append(f"issuetype in ({_quote_list(types)})")
    statuses = configured_statuses()
    if statuses:
        parts.append(f"status in ({_quote_list(statuses)})")
    parts.append("labels is not EMPTY")
    return " AND ".join(parts) + " ORDER BY created DESC"
