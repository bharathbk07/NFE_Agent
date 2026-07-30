"""First-class LangChain tools for PE specialists (REST / knowledge — not MCP)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class _EmptyArgs(BaseModel):
    pass


class _IssueKeyArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key, e.g. SCRUM-1")


class _JiraSearchArgs(BaseModel):
    query: str = Field(
        default="",
        description="Free-text search (summary/description) e.g. create-claim SLA",
    )
    jql: str = Field(
        default="",
        description="Optional full JQL. When set, overrides free-text query.",
    )
    max_results: int = Field(default=15, ge=1, le=50)


class _SearchArgs(BaseModel):
    query: str = Field(description="Search query for local knowledge / RAG")
    app: str = Field(default="", description="Optional app id")
    flow: str = Field(default="", description="Optional flow id")


class _TrendsArgs(BaseModel):
    question: str = Field(description="User question about trends or KPIs")
    app: str = Field(default="", description="Optional app id")
    flow: str = Field(default="", description="Optional flow id")


class _SyncConfluenceArgs(BaseModel):
    question: str = Field(
        default="",
        description="User question (used for exclude_smoke / VU filters)",
    )
    app: str = Field(default="", description="App id")
    flow: str = Field(default="", description="Flow id e.g. create-claim or default")
    exclude_smoke: bool = Field(default=False, description="Drop smoke/deferred runs")
    min_vus: int = Field(default=0, description="Minimum VUs filter (0 = off)")


def _list_jira_stories_impl(*, assist_fallback: bool = True) -> str:
    """List Jira stories via REST; assist mode falls back when nfe-agent filter is empty."""
    try:
        from src.integrations.jira.labels import routing_label
        from src.nodes.jira_story import (
            _format_candidate_list,
            _list_assist_board_issues,
            _list_eligible_issues,
        )

        issues = _list_eligible_issues()
        label = routing_label()
        if issues:
            md = _format_candidate_list(issues, list_only=True)
            return json.dumps(
                {
                    "count": len(issues),
                    "markdown": md,
                    "keys": [i.key for i in issues],
                    "eligible": True,
                    "source": "tool:list_jira_stories",
                },
                default=str,
            )

        if not assist_fallback:
            md = (
                f"No matching issues in **To Do** / **In Progress** with label "
                f"`{label}`.\n\nAdd the `{label}` label to a story, or say "
                f"**work on SCRUM-1** with an explicit key."
            )
            return json.dumps(
                {
                    "count": 0,
                    "markdown": md,
                    "message": md,
                    "source": "tool:list_jira_stories",
                }
            )

        board = _list_assist_board_issues()
        if not board:
            md = (
                f"No issues found with label `{label}`, and no To Do / In Progress "
                "issues were returned from a broader board search.\n\n"
                "Check `JIRA_*` credentials and project access."
            )
            return json.dumps(
                {
                    "count": 0,
                    "markdown": md,
                    "message": md,
                    "source": "tool:list_jira_stories",
                }
            )

        lines = [
            f"None with required label `{label}` for NFE execute. "
            "Showing board **To Do / In Progress** anyway (assist list):",
            "",
        ]
        for issue in board:
            has_label = issue.has_label(label)
            tag = "eligible" if has_label else f"missing `{label}`"
            lines.append(
                f"* **{issue.key}** [{issue.status or '?'}] — "
                f"{issue.summary or '(no summary)'} _( {tag} )_"
            )
        lines.extend(
            [
                "",
                f"Add label `{label}` then say **work on {board[0].key}** to run one.",
            ]
        )
        md = "\n".join(lines)
        return json.dumps(
            {
                "count": len(board),
                "markdown": md,
                "keys": [i.key for i in board],
                "eligible": False,
                "fallback": True,
                "source": "tool:list_jira_stories",
            },
            default=str,
        )
    except Exception as exc:
        logger.warning("list_jira_stories failed: %s", exc)
        md = (
            "Could not list Jira stories via REST. "
            f"Check `JIRA_BASE_URL` / email / token. Detail: `{exc}`"
        )
        return json.dumps(
            {"error": str(exc), "markdown": md, "source": "tool:list_jira_stories"}
        )


def _get_jira_issue_impl(issue_key: str) -> str:
    try:
        from src.integrations.jira.client import JiraClient

        issue = JiraClient().get_issue(issue_key.strip())
        payload = {
            "key": issue.key,
            "summary": issue.summary,
            "status": issue.status,
            "labels": list(issue.labels or []),
            "source": "tool:get_jira_issue",
            "markdown": (
                f"**{issue.key}** [{issue.status or '?'}] — {issue.summary or ''}\n"
                f"Labels: {', '.join(issue.labels or []) or '(none)'}"
            ),
        }
        desc = (issue.description or "")[:500]
        if desc:
            payload["description_preview"] = desc
        return json.dumps(payload, default=str)
    except Exception as exc:
        logger.warning("get_jira_issue failed: %s", exc)
        return json.dumps({"error": str(exc), "issue_key": issue_key})


def _search_jira_impl(query: str = "", jql: str = "", max_results: int = 15) -> str:
    """Search Jira via REST JQL (not the nfe-agent To Do list)."""
    try:
        from src.integrations.jira.client import JiraClient
        from src.security.secrets import redact_text_for_llm

        client = JiraClient()
        jql_q = (jql or "").strip()
        if not jql_q:
            q = (query or "").strip().replace('"', " ")
            if not q:
                return json.dumps(
                    {
                        "count": 0,
                        "markdown": "Provide a search query or JQL.",
                        "source": "tool:search_jira",
                    }
                )
            # Escape quotes already stripped; keep JQL simple
            jql_q = (
                f'(summary ~ "{q}" OR description ~ "{q}" OR text ~ "{q}") '
                "ORDER BY updated DESC"
            )
        issues = client.search_jql(jql_q, max_results=max_results)
        lines = [
            f"Jira search (`{redact_text_for_llm(jql_q)[:180]}`) — {len(issues)} hit(s):",
            "",
        ]
        for issue in issues:
            lines.append(
                f"* **{issue.key}** [{issue.status or '?'}] "
                f"({issue.issue_type or 'issue'}) — {issue.summary or '(no summary)'}"
            )
        if not issues:
            lines.append("_No matching issues._")
        return json.dumps(
            {
                "count": len(issues),
                "jql": jql_q,
                "keys": [i.key for i in issues],
                "markdown": "\n".join(lines),
                "source": "tool:search_jira",
            },
            default=str,
        )
    except Exception as exc:
        logger.warning("search_jira failed: %s", exc)
        return json.dumps(
            {
                "error": str(exc),
                "markdown": (
                    "Jira search failed. Check `JIRA_BASE_URL` / email / token. "
                    f"Detail: `{exc}`"
                ),
                "source": "tool:search_jira",
            }
        )


def _get_jira_comments_impl(issue_key: str) -> str:
    """Fetch recent comments on a story (where NFE posts SLA findings)."""
    try:
        from src.integrations.jira.client import JiraClient
        from src.security.secrets import redact_text_for_llm

        key = issue_key.strip().upper()
        comments = JiraClient().list_comments(key, max_results=20)
        # Prefer recent NFE-looking comments
        recent = comments[-8:] if comments else []
        lines = [f"Recent comments on **{key}** ({len(comments)} total):", ""]
        if not recent:
            lines.append("_No comments on this issue._")
        for i, body in enumerate(recent, 1):
            snippet = redact_text_for_llm(body)[:1200]
            lines.append(f"### Comment {i}\n{snippet}\n")
        return json.dumps(
            {
                "key": key,
                "count": len(comments),
                "markdown": "\n".join(lines),
                "source": "tool:get_jira_comments",
            },
            default=str,
        )
    except Exception as exc:
        logger.warning("get_jira_comments failed: %s", exc)
        return json.dumps(
            {
                "error": str(exc),
                "issue_key": issue_key,
                "markdown": f"Could not load comments for `{issue_key}`: `{exc}`",
                "source": "tool:get_jira_comments",
            }
        )


def _search_knowledge_impl(query: str, app: str = "", flow: str = "") -> str:
    parts: List[str] = []
    try:
        from src.utils.knowledge_store import read_flow, read_overview

        if app and flow:
            card = read_flow(app, flow)
            if card:
                parts.append(f"### Flow card\n{card[:3000]}")
        if app:
            overview = read_overview(app)
            if overview:
                parts.append(f"### Overview\n{overview[:1500]}")
    except Exception as exc:
        parts.append(f"knowledge_markdown_error: {exc}")

    try:
        from src.utils.rag_store import query as rag_query

        hits = rag_query(query, app=app or None, top_k=5)
        if hits:
            parts.append("### RAG hits")
            for h in hits[:5]:
                text = h.get("text") or h.get("document") or str(h)
                parts.append(str(text)[:800])
    except Exception as exc:
        parts.append(f"rag_error: {exc}")

    if not parts:
        return json.dumps(
            {
                "message": "No local knowledge or RAG hits.",
                "markdown": "No local knowledge or RAG hits.",
                "source": "tool:search_knowledge",
            }
        )
    md = "\n\n".join(parts)[:8000]
    return json.dumps({"markdown": md, "source": "tool:search_knowledge"})


def _get_run_trends_impl(question: str, app: str = "", flow: str = "") -> str:
    try:
        from src.utils.perf_evidence import gather_evidence_for_question
        from src.utils.perf_trend import wants_confluence_evidence, wants_tool_refresh

        force = wants_tool_refresh(question) or wants_confluence_evidence(question)
        bundle = gather_evidence_for_question(
            question,
            app=app or "",
            flow=flow or "default",
            force_refresh=force,
        )
        if isinstance(bundle, dict):
            if bundle.get("trend_markdown") and "markdown" not in bundle:
                bundle = {**bundle, "markdown": bundle["trend_markdown"]}
            return json.dumps(
                {**bundle, "source": "tool:get_run_trends"}, default=str
            )[:8000]
        return json.dumps(
            {"markdown": str(bundle)[:8000], "source": "tool:get_run_trends"}
        )
    except Exception as exc:
        try:
            from src.utils.knowledge_store import list_run_history
            from src.utils.perf_trend import build_trend_table

            runs = list_run_history(app, flow) if app else []
            table = build_trend_table(runs) if runs else ""
            return json.dumps(
                {
                    "markdown": table or f"No run history ({exc})",
                    "source": "tool:get_run_trends",
                }
            )
        except Exception as exc2:
            return json.dumps({"error": str(exc2), "source": "tool:get_run_trends"})


def _sync_confluence_trends_impl(
    question: str = "",
    app: str = "",
    flow: str = "",
    exclude_smoke: bool = False,
    min_vus: int = 0,
) -> str:
    try:
        from src.utils.app_registry import resolve_evidence_scope
        from src.utils.perf_evidence import sync_confluence_and_build_report
        from src.utils.perf_trend import parse_trend_filters

        app, flow, target_url = resolve_evidence_scope(
            question=question, app=app, flow=flow
        )
        filters = parse_trend_filters(question)
        report = sync_confluence_and_build_report(
            app=app or "",
            flow=flow or "default",
            question=question,
            target_url=target_url,
            force=True,
            exclude_smoke=exclude_smoke or bool(filters.get("exclude_smoke")),
            min_vus=min_vus or int(filters.get("min_vus") or 0),
        )
        report["markdown"] = report.get("trend_markdown") or ""
        report["source"] = "tool:sync_confluence_trends"
        return json.dumps(report, default=str)[:10000]
    except Exception as exc:
        logger.warning("sync_confluence_trends failed: %s", exc)
        return json.dumps(
            {
                "error": str(exc),
                "markdown": f"Confluence trend sync failed: `{exc}`",
                "source": "tool:sync_confluence_trends",
            }
        )


def build_nfe_tools(
    *,
    include_jira: bool = True,
    include_knowledge: bool = True,
    include_trends: bool = True,
    default_app: str = "",
    default_flow: str = "",
) -> List[Any]:
    """Build product-owned tools for specialists."""
    tools: List[Any] = []
    if include_jira:
        tools.append(
            StructuredTool.from_function(
                name="list_jira_stories",
                description=(
                    "List Jira stories via REST. Prefers nfe-agent label; "
                    "falls back to board To Do/In Progress when empty."
                ),
                func=_list_jira_stories_impl,
                args_schema=_EmptyArgs,
            )
        )

        async def _list_async() -> str:
            return _list_jira_stories_impl()

        tools[-1].coroutine = _list_async

        async def _get_async(issue_key: str) -> str:
            return _get_jira_issue_impl(issue_key)

        tools.append(
            StructuredTool.from_function(
                name="get_jira_issue",
                description="Get a Jira issue summary/status/labels via REST (read-only).",
                func=_get_jira_issue_impl,
                args_schema=_IssueKeyArgs,
            )
        )
        tools[-1].coroutine = _get_async

        async def _search_jira_async(
            query: str = "", jql: str = "", max_results: int = 15
        ) -> str:
            return _search_jira_impl(query=query, jql=jql, max_results=max_results)

        tools.append(
            StructuredTool.from_function(
                name="search_jira",
                description=(
                    "Search Jira by free text or JQL (REST). Use for analysis/"
                    "SLA/related tickets — NOT list_jira_stories (that only lists "
                    "nfe-agent To Do/In Progress for execution)."
                ),
                func=lambda query="", jql="", max_results=15: _search_jira_impl(
                    query=query, jql=jql, max_results=max_results
                ),
                args_schema=_JiraSearchArgs,
            )
        )
        tools[-1].coroutine = _search_jira_async

        async def _comments_async(issue_key: str) -> str:
            return _get_jira_comments_impl(issue_key)

        tools.append(
            StructuredTool.from_function(
                name="get_jira_comments",
                description=(
                    "Get recent comments on a Jira issue. By default NFE posts "
                    "SLA/smoke findings as comments on the story that was run; "
                    "a separate analysis ticket only appears if create-on-fail "
                    "was authorized."
                ),
                func=_get_jira_comments_impl,
                args_schema=_IssueKeyArgs,
            )
        )
        tools[-1].coroutine = _comments_async

    if include_knowledge:

        async def _search_async(query: str, app: str = "", flow: str = "") -> str:
            return _search_knowledge_impl(
                query, app=app or default_app, flow=flow or default_flow
            )

        tools.append(
            StructuredTool.from_function(
                name="search_knowledge",
                description="Search local knowledge markdown and Chroma RAG.",
                func=lambda query, app="", flow="": _search_knowledge_impl(
                    query, app=app or default_app, flow=flow or default_flow
                ),
                args_schema=_SearchArgs,
            )
        )
        tools[-1].coroutine = _search_async

    if include_trends:

        async def _trends_async(question: str, app: str = "", flow: str = "") -> str:
            return _get_run_trends_impl(
                question, app=app or default_app, flow=flow or default_flow
            )

        tools.append(
            StructuredTool.from_function(
                name="get_run_trends",
                description=(
                    "Get run KPI / trend evidence. Refreshes from Confluence when asked."
                ),
                func=lambda question, app="", flow="": _get_run_trends_impl(
                    question, app=app or default_app, flow=flow or default_flow
                ),
                args_schema=_TrendsArgs,
            )
        )
        tools[-1].coroutine = _trends_async

        async def _sync_async(
            question: str = "",
            app: str = "",
            flow: str = "",
            exclude_smoke: bool = False,
            min_vus: int = 0,
        ) -> str:
            return _sync_confluence_trends_impl(
                question=question,
                app=app or default_app,
                flow=flow or default_flow,
                exclude_smoke=exclude_smoke,
                min_vus=min_vus,
            )

        tools.append(
            StructuredTool.from_function(
                name="sync_confluence_trends",
                description=(
                    "ALWAYS use for Confluence-based trend analysis: sync Run pages "
                    "from Confluence, ingest KPIs, return a trend markdown table. "
                    "Never stop at page links."
                ),
                func=lambda question="", app="", flow="", exclude_smoke=False, min_vus=0: (
                    _sync_confluence_trends_impl(
                        question=question,
                        app=app or default_app,
                        flow=flow or default_flow,
                        exclude_smoke=exclude_smoke,
                        min_vus=min_vus,
                    )
                ),
                args_schema=_SyncConfluenceArgs,
            )
        )
        tools[-1].coroutine = _sync_async

    return tools
