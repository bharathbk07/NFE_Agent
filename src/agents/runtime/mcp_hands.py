"""Atlassian MCP helpers for PE Agent Hands (Jira list/search without REST)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Prefer JQL / issue-search style tools from Atlassian Rovo MCP
_SEARCH_NAME_HINTS = (
    "searchjiraissuesusingjql",
    "search_jira",
    "jirasearch",
    "jql",
    "searchissues",
    "getvisiblejirasites",  # sometimes needed first
    "search",
)


def _tool_rank(name: str) -> int:
    n = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if "searchjiraissuesusingjql" in n or n.endswith("jql"):
        return 0
    if "jira" in n and "search" in n:
        return 1
    if "search" in n and "confluence" not in n:
        return 2
    if "issue" in n and "get" in n:
        return 3
    return 9


def pick_jira_search_tools(tools: List[Any], *, limit: int = 4) -> List[Any]:
    scored: List[Tuple[int, Any]] = []
    for tool in tools or []:
        name = getattr(tool, "name", "") or ""
        lower = name.lower()
        if "confluence" in lower and "jira" not in lower:
            continue
        rank = _tool_rank(name)
        if rank >= 9 and not any(h in re.sub(r"[^a-z0-9]", "", lower) for h in _SEARCH_NAME_HINTS):
            continue
        scored.append((rank, tool))
    scored.sort(key=lambda x: (x[0], getattr(x[1], "name", "")))
    return [t for _, t in scored[:limit]]


def _result_to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return json.dumps(raw, default=str)
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("text"):
                parts.append(str(block.get("text")))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(raw)


async def invoke_mcp_tool(tool: Any, args: Dict[str, Any]) -> str:
    try:
        if hasattr(tool, "ainvoke"):
            raw = await tool.ainvoke(args)
        else:
            raw = tool.invoke(args)
        return _result_to_text(raw)[:12000]
    except Exception as exc:
        logger.warning("MCP tool %s failed: %s", getattr(tool, "name", "?"), exc)
        return json.dumps({"error": str(exc), "tool": getattr(tool, "name", "")})


def _candidate_arg_sets(jql: str, query: str) -> List[Dict[str, Any]]:
    """Try common Atlassian MCP arg shapes."""
    return [
        {"jql": jql, "maxResults": 25},
        {"jql": jql, "max_results": 25},
        {"cloudId": "", "jql": jql, "maxResults": 25},
        {"query": query, "maxResults": 25},
        {"searchString": query},
        {"jql": jql},
        {},
    ]


async def list_jira_issues_via_mcp(
    *,
    jql: str = 'statusCategory != Done ORDER BY updated DESC',
    query: str = "issues",
) -> Dict[str, Any]:
    """Discover Atlassian MCP tools and run a Jira issue search.

    Returns dict with ok, markdown, tool_calls, raw snippets.
    """
    from src.tools.capability_tools import get_tools_for_capabilities

    tools = await get_tools_for_capabilities(["jira", "alm", "confluence"])
    jira_tools = [
        t
        for t in tools
        if "confluence" not in (getattr(t, "name", "") or "").lower()
        or "jira" in (getattr(t, "name", "") or "").lower()
    ]
    if not jira_tools:
        return {
            "ok": False,
            "markdown": (
                "No Atlassian MCP Jira tools are available. "
                "Enable `atlassian` in `config/mcp_servers.json` and complete "
                "MCP auth (mcp-remote browser login) on first use."
            ),
            "tool_calls": [],
            "available_tools": [],
        }

    search_tools = pick_jira_search_tools(jira_tools)
    if not search_tools:
        names = [getattr(t, "name", "") for t in jira_tools[:20]]
        return {
            "ok": False,
            "markdown": (
                "Atlassian MCP connected but no Jira search tool matched. "
                f"Available tools: {', '.join(names) or '(none)'}"
            ),
            "tool_calls": [],
            "available_tools": names,
        }

    used: List[str] = []
    snippets: List[str] = []
    for tool in search_tools:
        name = getattr(tool, "name", "") or "mcp_tool"
        for args in _candidate_arg_sets(jql, query):
            text = await invoke_mcp_tool(tool, args)
            used.append(name)
            if not text:
                continue
            if '"error"' in text[:200] and "issues" not in text.lower():
                # try next arg shape
                snippets.append(f"_{name}_ args={args}: {text[:300]}")
                continue
            # Success-ish
            md = (
                f"### Jira via MCP (`{name}`)\n\n"
                f"JQL/query tried: `{jql}`\n\n"
                f"{text[:8000]}"
            )
            return {
                "ok": True,
                "markdown": md,
                "tool_calls": used,
                "tool": name,
                "raw": text[:4000],
            }

    names = [getattr(t, "name", "") for t in search_tools]
    return {
        "ok": False,
        "markdown": (
            "Atlassian MCP search did not return issues. "
            "Complete mcp-remote OAuth if prompted, then retry.\n\n"
            f"Tried tools: {', '.join(names)}\n\n"
            + "\n".join(snippets[:5])
        ),
        "tool_calls": used,
        "available_tools": names,
    }


def mcp_tools_catalog_lines(tools: List[Any]) -> str:
    lines = []
    for tool in tools or []:
        name = getattr(tool, "name", "") or ""
        desc = (getattr(tool, "description", None) or "")[:140]
        lines.append(f"- `{name}` [mcp/read] — {desc}")
    return "\n".join(lines)
