"""KnowledgeQA specialist — PE concepts + RAG anytime."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from src.agents.runtime.base import SubAgent
from src.agents.runtime.contracts import Citation, HandoffResult
from src.agents.runtime.loop import run_tool_loop
from src.tools.pe_assistant_tools import build_nfe_tools
from src.utils.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


class KnowledgeQAAgent(SubAgent):
    id = "knowledge_qa"
    accepts_capabilities: Sequence[str] = ("knowledge", "rag")

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
        tools: List[Any] = []
        if need_tools:
            tools = build_nfe_tools(
                include_jira=False,
                include_knowledge=True,
                include_trends=False,
                default_app=str(context_pack.get("app") or ""),
                default_flow=str(context_pack.get("flow") or ""),
            )
        catalog = ", ".join(getattr(t, "name", "") for t in tools) or "(none)"
        assembled = self.builder.build(
            role="knowledge_qa",
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
        citations = [
            Citation(source="knowledge", ref="context_pack"),
        ]
        if context_pack.get("has_session"):
            citations.append(Citation(source="session", ref="context_pack"))
        if any(n == "search_knowledge" for n in result["tool_calls"]):
            citations.append(Citation(source="tool", ref="search_knowledge"))
        return HandoffResult(
            specialist="knowledge_qa",
            answer_md=result["text"] or "",
            citations=citations,
            tool_calls=list(result["tool_calls"]),
            confidence=0.75 if result["text"] else 0.4,
            missing=[],
        )
