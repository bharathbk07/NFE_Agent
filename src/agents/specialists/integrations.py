"""Integrations specialist — Jira REST (+ optional MCP enrichment)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Sequence

from src.agents.runtime.base import SubAgent
from src.agents.runtime.contracts import Citation, HandoffResult
from src.agents.runtime.loop import run_tool_loop
from src.tools.pe_assistant_tools import build_nfe_tools
from src.utils.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


def wants_analysis_ticket_question(question: str) -> bool:
    """True when the user asks if NFE created an analysis / SLA failure ticket."""
    q = (question or "").lower()
    if not re.search(r"\b(jira|ticket|issue|story)\b", q):
        return False
    return bool(
        re.search(
            r"\b("
            r"creat(e|ed|ing)|did\s+u\s+creat|did\s+you\s+creat|"
            r"open(ed)?|rais(e|ed)|fil(e|ed)"
            r")\b.{0,60}\b("
            r"analys|analysis|rca|sla|fail|failure|why"
            r")\b|"
            r"\b(analys|analysis|rca)\b.{0,40}\b(ticket|issue|jira)\b|"
            r"\b(sla|fail).{0,40}\b(ticket|issue)\b",
            q,
        )
    )


def _story_key_from_pack(context_pack: Dict[str, Any], question: str) -> str:
    from src.agents.intent_router import ISSUE_KEY_RE

    blob = f"{question}\n{context_pack.get('session_json') or ''}\n{context_pack.get('knowledge_md') or ''}"
    m = ISSUE_KEY_RE.search(blob)
    return m.group(1) if m else ""


class IntegrationsAgent(SubAgent):
    id = "integrations"
    accepts_capabilities: Sequence[str] = ("jira", "confluence", "alm")

    def __init__(self) -> None:
        self.builder = PromptBuilder()

    async def run(
        self,
        *,
        goal: str,
        question: str,
        context_pack: Dict[str, Any],
        need_tools: bool = True,
    ) -> HandoffResult:
        from src.agents.intent_router import wants_jira_list
        from src.utils.perf_trend import wants_confluence_evidence, wants_trend_question

        # Confluence/trend questions do not belong here
        if wants_confluence_evidence(question) or (
            wants_trend_question(question) and "jira" not in (question or "").lower()
        ):
            return HandoffResult(
                specialist="integrations",
                answer_md=(
                    "Trend / Confluence analysis is handled by the EvidenceTrends "
                    "specialist (sync Run pages → KPI table). Re-ask as a trend "
                    "question if you did not get a table."
                ),
                citations=[],
                tool_calls=[],
                confidence=0.4,
                missing=["route_to_evidence_trends"],
            )

        # Fast path: analysis-ticket questions — product fact + search + comments
        if need_tools and wants_analysis_ticket_question(question):
            return await self._answer_analysis_ticket(question, context_pack, goal)

        if need_tools and wants_jira_list(question) and not wants_analysis_ticket_question(
            question
        ):
            from src.tools.pe_assistant_tools import _list_jira_stories_impl

            raw = _list_jira_stories_impl(assist_fallback=True)
            try:
                data = json.loads(raw)
            except Exception:
                data = {"markdown": raw}
            if data.get("error") and not data.get("markdown"):
                return HandoffResult(
                    specialist="integrations",
                    answer_md=(
                        "I couldn’t reach Jira via REST. Check `JIRA_BASE_URL`, "
                        "`JIRA_EMAIL`, and `JIRA_API_TOKEN`.\n\n"
                        f"Detail: `{data.get('error')}`"
                    ),
                    citations=[Citation(source="tool", ref="list_jira_stories")],
                    tool_calls=["list_jira_stories"],
                    confidence=0.5,
                    missing=["jira_rest_config"],
                )
            md = data.get("markdown") or data.get("message") or json.dumps(data, indent=2)
            return HandoffResult(
                specialist="integrations",
                answer_md=md,
                citations=[Citation(source="tool", ref="list_jira_stories")],
                tool_calls=["list_jira_stories"],
                confidence=0.9,
                missing=[],
            )

        # For non-list Jira questions, use REST tools first; MCP only as enrichment
        tools: List[Any] = []
        if need_tools:
            tools = build_nfe_tools(
                include_jira=True,
                include_knowledge=False,
                include_trends=False,
            )
            try:
                from src.tools.capability_tools import get_tools_for_capabilities

                mcp_tools = await get_tools_for_capabilities(
                    self.accepts_capabilities, allow_writes=False
                )
                # Cap MCP flood — prefer REST for product truth
                tools.extend(list(mcp_tools)[:8])
            except Exception as exc:
                logger.debug("MCP enrichment skipped: %s", exc)

        catalog = ", ".join(getattr(t, "name", "") for t in tools) or "(none)"
        assembled = self.builder.build(
            role="integrations",
            question=question,
            context_pack=context_pack,
            tool_catalog=catalog,
            goal=goal,
        )
        result = await run_tool_loop(
            system=assembled.system,
            user=assembled.user,
            tools=tools,
            max_rounds=3,
        )
        citations = [Citation(source="tool", ref="integrations")]
        for name in result["tool_calls"]:
            src = "tool"
            if name and any(
                name.startswith(p) for p in ("atlassian", "jira_", "confluence")
            ):
                if not name.startswith(("list_jira", "get_jira", "search_jira")):
                    src = "mcp"
            citations.append(Citation(source=src, ref=name or "tool"))
        return HandoffResult(
            specialist="integrations",
            answer_md=result["text"] or "",
            citations=citations[:8],
            tool_calls=list(result["tool_calls"]),
            confidence=0.8 if result["text"] else 0.4,
            missing=[],
        )

    async def _answer_analysis_ticket(
        self,
        question: str,
        context_pack: Dict[str, Any],
        goal: str,
    ) -> HandoffResult:
        from src.tools.pe_assistant_tools import (
            _get_jira_comments_impl,
            _search_jira_impl,
        )

        parts: List[str] = [
            "**Default policy:** NFE does **not** auto-create a separate analysis "
            "Jira issue when SLA/smoke fails — unless you authorized create-on-fail "
            "(and `NFE_JIRA_CREATE_ENABLED` is on).",
            "",
            "What it does by default:",
            "- Posts **findings as comments** on the story that was executed "
            "(e.g. SCRUM-1).",
            "- Publishes a **Confluence Run page** with KPI / SLA details when credentials allow.",
            "",
        ]
        tool_calls: List[str] = []
        citations = [Citation(source="knowledge", ref="jira_pipeline_comments")]

        # Search for related tickets
        flow = str(context_pack.get("flow") or "create-claim")
        search_q = f"{flow} SLA OR {flow} fail OR create-claim performance"
        raw_search = _search_jira_impl(query=search_q, max_results=10)
        tool_calls.append("search_jira")
        citations.append(Citation(source="tool", ref="search_jira"))
        try:
            search = json.loads(raw_search)
        except Exception:
            search = {"markdown": raw_search}
        parts.append("### Related Jira search")
        parts.append(str(search.get("markdown") or "_search failed_"))
        parts.append("")

        key = _story_key_from_pack(context_pack, question)
        if not key:
            # Prefer first search hit that looks like a story
            keys = search.get("keys") or []
            if keys:
                key = str(keys[0])
        if key:
            raw_c = _get_jira_comments_impl(key)
            tool_calls.append("get_jira_comments")
            citations.append(Citation(source="tool", ref="get_jira_comments"))
            try:
                comments = json.loads(raw_c)
            except Exception:
                comments = {"markdown": raw_c}
            parts.append(f"### Comments on `{key}` (where findings are posted)")
            parts.append(str(comments.get("markdown") or "_no comments_"))
        else:
            parts.append(
                "I could not infer a story key from this chat. "
                "Say **get comments for SCRUM-1** (or your issue key) to see the "
                "SLA findings comment."
            )

        _ = goal
        return HandoffResult(
            specialist="integrations",
            answer_md="\n".join(parts),
            citations=citations[:8],
            tool_calls=tool_calls,
            confidence=0.92,
            missing=[],
        )
