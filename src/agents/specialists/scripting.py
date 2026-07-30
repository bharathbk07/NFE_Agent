"""ScriptingAdvise specialist — k6 / IR / assertions / correlation advice."""
from __future__ import annotations

import logging
from typing import Any, Dict, Sequence

from src.agents.runtime.base import SubAgent
from src.agents.runtime.contracts import Citation, HandoffResult
from src.agents.runtime.loop import run_tool_loop
from src.utils.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)


class ScriptingAdviseAgent(SubAgent):
    id = "scripting"
    accepts_capabilities: Sequence[str] = ("scripting", "k6")

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
        assembled = self.builder.build(
            role="scripting",
            question=question,
            context_pack=context_pack,
            tool_catalog="(session artifacts in context; no live tools)",
            goal=goal,
        )
        result = await run_tool_loop(
            system=assembled.system,
            user=assembled.user,
            tools=[],
            max_rounds=1,
        )
        citations = []
        if context_pack.get("has_session"):
            citations.append(Citation(source="session", ref="ir_k6"))
        missing = []
        if not context_pack.get("has_session"):
            missing.append(
                "No session script/IR yet — capture a journey or reuse a recording first."
            )
        return HandoffResult(
            specialist="scripting",
            answer_md=result["text"] or "",
            citations=citations,
            tool_calls=[],
            confidence=0.75 if result["text"] else 0.4,
            missing=missing,
        )
