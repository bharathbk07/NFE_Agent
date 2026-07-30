"""Deterministic assist shortcuts — reliable Hands without an LLM round-trip.

List-Jira prefers MCP when the user asks for MCP, and falls back to REST.
Never answers with AnalysisQA empty-session text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_LIST_JIRA = re.compile(
    r"\b(list|show|display|search)\b.*\b(jira|jirs|stor\w*|issues?|tickets?)\b"
    r"|\b(jira|jirs|stories|issues)\b.*\b(list|available|open|search|mcp)\b"
    r"|\ball\s+(jira\s+)?stor"
    r"|\bmcp\b.*\b(jira|jirs|stor|issue)",
    re.I,
)
_WANTS_MCP = re.compile(r"\bmcp\b", re.I)


def is_list_jira_request(question: str) -> bool:
    return bool(_LIST_JIRA.search((question or "").strip()))


def wants_mcp(question: str) -> bool:
    return bool(_WANTS_MCP.search(question or ""))


def _result(
    *,
    answer: str,
    tool_calls: list,
    authorizations: Optional[list],
    reason: str,
    **extra: Any,
) -> Dict[str, Any]:
    return {
        "answer": answer,
        "tool_calls": tool_calls,
        "pending_action": None,
        "agent_authorizations": list(authorizations or []),
        "context_sources": ["tools", "mcp"] if any("mcp" in t for t in tool_calls) else ["tools"],
        "plan": {"reason": reason, **extra},
    }


async def _via_mcp(authorizations: Optional[list]) -> Dict[str, Any]:
    from src.agents.runtime.mcp_hands import list_jira_issues_via_mcp

    data = await list_jira_issues_via_mcp(
        jql="statusCategory != Done ORDER BY updated DESC",
        query="jira issues stories",
    )
    calls = [f"mcp:{c}" for c in (data.get("tool_calls") or [])] or ["mcp:atlassian"]
    return _result(
        answer=str(data.get("markdown") or "_No MCP result._"),
        tool_calls=calls,
        authorizations=authorizations,
        reason="fastpath_list_jira_mcp" if data.get("ok") else "fastpath_list_jira_mcp_empty",
        ok=bool(data.get("ok")),
    )


async def _via_rest(authorizations: Optional[list]) -> Dict[str, Any]:
    from src.tools.pe_assistant_tools import _list_jira_stories_impl

    try:
        raw = await asyncio.to_thread(
            lambda: _list_jira_stories_impl(assist_fallback=True)
        )
    except Exception as exc:
        logger.warning("list_jira_stories REST failed: %s", exc)
        return _result(
            answer=(
                "REST Jira list failed "
                f"({type(exc).__name__}). Check `JIRA_*` in `.env`, "
                "or say **use MCP to list Jira issues**."
            ),
            tool_calls=["list_jira_stories"],
            authorizations=authorizations,
            reason="fastpath_list_jira_rest_error",
        )
    try:
        data = json.loads(raw)
    except Exception:
        data = {"markdown": raw, "count": 0}
    count = int(data.get("count") or 0)
    md = str(data.get("markdown") or data.get("message") or "").strip()
    return _result(
        answer=md or "_No stories returned from REST._",
        tool_calls=["list_jira_stories"],
        authorizations=authorizations,
        reason="fastpath_list_jira_rest",
        count=count,
        keys=data.get("keys"),
    )


async def try_assist_fast_path(
    question: str,
    *,
    authorizations: Optional[list] = None,
) -> Optional[Dict[str, Any]]:
    """Return a complete PE agent result dict, or None to continue to the Brain."""
    q = (question or "").strip()
    if not q:
        return None

    if not is_list_jira_request(q):
        return None

    prefer_mcp = wants_mcp(q)

    if prefer_mcp:
        # User explicitly asked for MCP — do not substitute REST dumps
        return await _via_mcp(authorizations)

    # Default: REST first (product execute path), then MCP if empty
    rest = await _via_rest(authorizations)
    if int((rest.get("plan") or {}).get("count") or 0) > 0:
        return rest

    mcp_out = await _via_mcp(authorizations)
    if mcp_out.get("plan", {}).get("ok"):
        return mcp_out

    # Both empty — honest combined message
    combined = (
        "### Jira list\n\n"
        "**REST:** " + (rest.get("answer") or "empty") + "\n\n"
        "**MCP:** " + (mcp_out.get("answer") or "empty") + "\n\n"
        "Tips: set a real `JIRA_EMAIL` / `JIRA_API_TOKEN` for REST, "
        "or complete Atlassian MCP (`mcp-remote`) login for MCP search."
    )
    return _result(
        answer=combined,
        tool_calls=list(rest.get("tool_calls") or [])
        + list(mcp_out.get("tool_calls") or []),
        authorizations=authorizations,
        reason="fastpath_list_jira_both_empty",
    )
