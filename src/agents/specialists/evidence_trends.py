"""EvidenceTrends specialist — smoke / KPI / trends / Confluence sync reports."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Sequence

from src.agents.runtime.base import SubAgent
from src.agents.runtime.contracts import Citation, HandoffResult
from src.agents.runtime.loop import run_tool_loop
from src.tools.pe_assistant_tools import (
    _sync_confluence_trends_impl,
    build_nfe_tools,
)
from src.utils.perf_trend import (
    parse_trend_filters,
    wants_confluence_evidence,
    wants_tool_refresh,
    wants_trend_question,
)
from src.utils.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


class EvidenceTrendsAgent(SubAgent):
    id = "evidence_trends"
    accepts_capabilities: Sequence[str] = ("monitoring", "confluence", "trends")

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
        app = str(context_pack.get("app") or "")
        flow = str(context_pack.get("flow") or "default")
        try:
            from src.utils.app_registry import resolve_evidence_scope

            app, flow, _url = resolve_evidence_scope(
                question=question,
                app=app,
                flow=flow,
                target_url=str(context_pack.get("target_url") or ""),
            )
        except Exception:
            pass
        want_conf = wants_confluence_evidence(question) or wants_tool_refresh(question)
        want_trend = wants_trend_question(question) or "trend" in (goal or "").lower()

        # Fast path: Confluence → KPI trend report (never stop at page links)
        # Always take this path for trend asks so empty session scope cannot
        # strand the answer on local smoke alone.
        if need_tools and (want_conf or want_trend):
            filters = parse_trend_filters(question)
            raw = _sync_confluence_trends_impl(
                question=question,
                app=app,
                flow=flow or "default",
                exclude_smoke=bool(filters.get("exclude_smoke")),
                min_vus=int(filters.get("min_vus") or 0),
            )
            try:
                data = json.loads(raw)
            except Exception:
                data = {"markdown": raw}
            md = data.get("trend_markdown") or data.get("markdown") or raw
            citations = [Citation(source="tool", ref="sync_confluence_trends")]
            if data.get("synced_count"):
                citations.append(Citation(source="adapter", ref="confluence_sync"))
            return HandoffResult(
                specialist="evidence_trends",
                answer_md=str(md),
                citations=citations,
                tool_calls=["sync_confluence_trends"],
                confidence=0.9 if data.get("synced_count") or data.get("kpis") else 0.55,
                missing=[]
                if (data.get("synced_count") or data.get("kpis"))
                else ["confluence_sync_empty"],
            )

        tools: List[Any] = []
        if need_tools:
            tools = build_nfe_tools(
                include_jira=False,
                include_knowledge=True,
                include_trends=True,
                default_app=app,
                default_flow=flow,
            )
        catalog = ", ".join(getattr(t, "name", "") for t in tools) or "(none)"
        assembled = self.builder.build(
            role="evidence_trends",
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
        citations = []
        if context_pack.get("has_session"):
            citations.append(Citation(source="session", ref="k6_smoke"))
        for name in result["tool_calls"]:
            citations.append(Citation(source="tool", ref=name))
        missing = []
        if not context_pack.get("has_session") and not result["tool_calls"]:
            missing.append(
                "No session smoke yet — sync Confluence or run a Jira story / analyse."
            )
        return HandoffResult(
            specialist="evidence_trends",
            answer_md=result["text"] or "",
            citations=citations,
            tool_calls=list(result["tool_calls"]),
            confidence=0.8 if result["text"] else 0.4,
            missing=missing,
        )
