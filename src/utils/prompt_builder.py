"""Compose PE assistant prompts from role templates + shared context layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.agents.runtime.context_pack import pack_to_prompt_block, slice_for_specialist
from src.security.secrets import redact_text_for_llm
from src.utils.prompt_loader import load_prompt_text, render_prompt


@dataclass
class AssembledPrompt:
    """Renderable prompt package for a supervisor or specialist turn."""

    system: str
    user: str
    role: str
    evidence_meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def messages(self) -> List[Any]:
        return [
            SystemMessage(content=self.system),
            HumanMessage(content=self.user),
        ]

    @property
    def combined(self) -> str:
        return f"{self.system}\n\n{self.user}"


class PromptBuilder:
    """Build layered prompts for PE supervisor and specialists."""

    ROLE_TEMPLATES = {
        "supervisor": "agents/supervisor",
        "supervisor_synthesize": "agents/supervisor_synthesize",
        "knowledge_qa": "agents/knowledge_qa",
        "evidence_trends": "agents/evidence_trends",
        "integrations": "agents/integrations",
        "scripting": "agents/scripting",
    }

    def build(
        self,
        *,
        role: str,
        question: str,
        context_pack: Dict[str, Any],
        tool_catalog: str = "",
        goal: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> AssembledPrompt:
        """Compose system + user messages for ``role``.

        Args:
            role: Template key (supervisor, knowledge_qa, …).
            question: User question.
            context_pack: Full or already-sliced pack.
            tool_catalog: Human-readable tool list for the role.
            goal: Specialist goal from the plan.
            extra: Extra template placeholders (e.g. specialist_results).
        """
        template = self.ROLE_TEMPLATES.get(role, role)
        specialist_id = role if role in (
            "knowledge_qa",
            "evidence_trends",
            "integrations",
            "scripting",
        ) else ""
        slice_ = (
            slice_for_specialist(context_pack, specialist_id)
            if specialist_id
            else {
                "question": question,
                "app": context_pack.get("app"),
                "flow": context_pack.get("flow"),
                "has_session": context_pack.get("has_session"),
                "session_json": str(context_pack.get("session_json") or "")[:8000],
                "knowledge_md": str(context_pack.get("knowledge_md") or "")[:6000],
                "evidence_sources": context_pack.get("evidence_sources") or [],
            }
        )
        context_block = pack_to_prompt_block(slice_)
        values = {
            "question": question or "",
            "goal": goal or question or "",
            "context": context_block,
            "tool_catalog": tool_catalog or "(none)",
            "app": slice_.get("app") or "",
            "flow": slice_.get("flow") or "",
            "has_session": str(bool(slice_.get("has_session"))),
            "specialist_results": (extra or {}).get("specialist_results", ""),
        }
        try:
            system = render_prompt(template, **values)
        except FileNotFoundError:
            system = load_prompt_text("analysis_qa").format(
                context=context_block,
                knowledge=slice_.get("knowledge_md") or "",
                question=question,
            )
        except KeyError:
            # Template may not use every placeholder
            raw = load_prompt_text(template)
            system = raw
            for key, val in values.items():
                system = system.replace("{" + key + "}", str(val))

        system = redact_text_for_llm(system)
        if len(system) > 16000:
            system = system[:16000] + "\n... [truncated]"

        user = (
            f"Goal: {goal}\n\nUser question:\n{question}"
            if goal
            else f"User question:\n{question}"
        )
        if extra and extra.get("specialist_results"):
            user += (
                "\n\nSpecialist results to synthesize:\n"
                + str(extra["specialist_results"])[:10000]
            )

        return AssembledPrompt(
            system=system,
            user=redact_text_for_llm(user),
            role=role,
            evidence_meta={
                "sources": list(slice_.get("evidence_sources") or []),
                "app": slice_.get("app"),
                "flow": slice_.get("flow"),
                "has_session": bool(slice_.get("has_session")),
            },
        )
