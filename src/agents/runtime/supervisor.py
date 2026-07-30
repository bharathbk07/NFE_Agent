"""PE Director supervisor: plan → specialists → synthesize."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from src.agents.runtime.contracts import HandoffResult, PlanStep, SupervisorPlan
from src.agents.runtime.context_pack import build_context_pack, pack_to_prompt_block
from src.agents.runtime.registry import ensure_default_agents, get_sub_agent
from src.utils.model_router import TaskType, get_model_router
from src.utils.prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

MAX_PLAN_STEPS = 4
MAX_SIMPLE_SPECIALISTS = 2


class PESupervisor:
    """Personal PE assistant supervisor (planner + synthesizer)."""

    def __init__(self, builder: Optional[PromptBuilder] = None) -> None:
        self.builder = builder or PromptBuilder()

    async def run(self, state: Dict[str, Any], question: str) -> Dict[str, Any]:
        """Run one assist turn and return message + meta.

        Returns:
            ``{answer, plan, handoffs, context_sources}``
        """
        ensure_default_agents()
        pack = build_context_pack(state, question)
        plan = await self._plan(question, pack)
        logger.info(
            "supervisor.plan steps=%s direct=%s reason=%s",
            [s.specialist for s in plan.steps],
            bool(plan.direct_answer),
            (plan.reason or "")[:120],
        )

        if plan.direct_answer:
            return {
                "answer": plan.direct_answer,
                "plan": plan.model_dump(),
                "handoffs": [],
                "context_sources": list(pack.get("evidence_sources") or []),
            }

        steps = plan.steps[:MAX_PLAN_STEPS]
        if len(steps) > MAX_SIMPLE_SPECIALISTS and _looks_simple(question):
            steps = steps[:MAX_SIMPLE_SPECIALISTS]

        handoffs = await self._run_steps(steps, question, pack)
        if not handoffs:
            # Fallback: knowledge_qa alone
            agent = get_sub_agent("knowledge_qa")
            if agent:
                handoffs = [
                    await agent.run(
                        goal="Answer the user as a PE assistant",
                        question=question,
                        context_pack=pack,
                        need_tools=True,
                    )
                ]

        if plan.synthesize and len(handoffs) > 1:
            answer = await self._synthesize(question, pack, handoffs)
        elif handoffs:
            answer = handoffs[0].answer_md
            if handoffs[0].missing:
                answer += "\n\n" + _format_missing(handoffs[0].missing)
            answer += _format_citations(handoffs)
        else:
            answer = (
                "I couldn’t gather enough evidence to answer that yet. "
                "Try **list jira stories**, paste a journey URL, or ask about "
                "performance testing concepts."
            )

        sources = list(pack.get("evidence_sources") or [])
        for h in handoffs:
            for c in h.citations:
                if c.source not in sources:
                    sources.append(c.source)

        return {
            "answer": answer,
            "plan": plan.model_dump(),
            "handoffs": [h.model_dump() for h in handoffs],
            "context_sources": sources,
        }

    async def _plan(self, question: str, pack: Dict[str, Any]) -> SupervisorPlan:
        assembled = self.builder.build(
            role="supervisor",
            question=question,
            context_pack=pack,
            tool_catalog=_supervisor_tool_catalog(),
        )
        router = get_model_router()
        try:
            decision = await router.ainvoke_with_failover(
                TaskType.ORCHESTRATION,
                lambda model: model.with_structured_output(
                    SupervisorPlan, method="json_schema"
                ),
                assembled.messages,
            )
            if isinstance(decision, SupervisorPlan):
                return _normalize_plan(decision, question, pack)
            if isinstance(decision, dict):
                return _normalize_plan(
                    SupervisorPlan.model_validate(decision), question, pack
                )
        except Exception as exc:
            logger.warning("supervisor.plan failed (%s); heuristic plan", exc)
        return _heuristic_plan(question, pack)

    async def _run_steps(
        self,
        steps: List[PlanStep],
        question: str,
        pack: Dict[str, Any],
    ) -> List[HandoffResult]:
        async def _one(step: PlanStep) -> Optional[HandoffResult]:
            agent = get_sub_agent(step.specialist)
            if not agent:
                logger.warning("Unknown specialist %s", step.specialist)
                return None
            logger.info("specialist.%s goal=%s", step.specialist, step.goal[:80])
            try:
                return await agent.run(
                    goal=step.goal,
                    question=question,
                    context_pack=pack,
                    need_tools=step.need_tools,
                )
            except Exception as exc:
                logger.warning("specialist.%s failed: %s", step.specialist, exc)
                return HandoffResult(
                    specialist=step.specialist,
                    answer_md="",
                    confidence=0.2,
                    missing=[f"{step.specialist} failed: {exc}"],
                )

        # Parallel when independent
        results = await asyncio.gather(*[_one(s) for s in steps])
        return [r for r in results if r is not None]

    async def _synthesize(
        self,
        question: str,
        pack: Dict[str, Any],
        handoffs: List[HandoffResult],
    ) -> str:
        fragments = []
        for h in handoffs:
            fragments.append(
                f"### {h.specialist} (confidence={h.confidence:.2f})\n{h.answer_md}"
            )
            if h.missing:
                fragments.append("Missing: " + "; ".join(h.missing))
        body = "\n\n".join(fragments)
        assembled = self.builder.build(
            role="supervisor_synthesize",
            question=question,
            context_pack=pack,
            extra={"specialist_results": body[:10000]},
        )
        router = get_model_router()
        try:
            raw = await router.ainvoke_with_failover(
                TaskType.ORCHESTRATION,
                lambda model: model,
                assembled.messages,
            )
            text = getattr(raw, "content", None) or str(raw)
            if isinstance(text, list):
                text = "\n".join(
                    b.get("text", "") if isinstance(b, dict) else str(b) for b in text
                )
            out = str(text).strip()
            out += _format_citations(handoffs)
            return out
        except Exception as exc:
            logger.warning("synthesize failed: %s", exc)
            return body + _format_citations(handoffs)


def _normalize_plan(
    plan: SupervisorPlan, question: str, pack: Dict[str, Any]
) -> SupervisorPlan:
    from src.agents.specialists.integrations import wants_analysis_ticket_question

    if wants_analysis_ticket_question(question):
        return _heuristic_plan(question, pack)

    if plan.direct_answer and not plan.steps:
        return plan
    # Drop unknown / empty steps
    cleaned: List[PlanStep] = []
    for step in plan.steps[:MAX_PLAN_STEPS]:
        if get_sub_agent(step.specialist) or step.specialist in (
            "knowledge_qa",
            "evidence_trends",
            "integrations",
            "scripting",
        ):
            cleaned.append(step)
    if not cleaned and not plan.direct_answer:
        return _heuristic_plan(question, pack)
    plan.steps = cleaned
    return plan


def _heuristic_plan(question: str, pack: Dict[str, Any]) -> SupervisorPlan:
    q = (question or "").lower()
    steps: List[PlanStep] = []

    from src.agents.specialists.integrations import wants_analysis_ticket_question
    from src.utils.perf_trend import wants_confluence_evidence, wants_trend_question

    # Confluence / trend analysis → EvidenceTrends only (never Integrations MCP links)
    if wants_confluence_evidence(q) or wants_trend_question(q):
        steps.append(
            PlanStep(
                specialist="evidence_trends",
                goal=(
                    "Sync Confluence Run pages if needed and return a KPI trend "
                    "markdown table (not page links only)"
                ),
                need_tools=True,
            )
        )
        return SupervisorPlan(
            steps=steps,
            synthesize=False,
            reason="heuristic_confluence_or_trend",
        )

    if wants_analysis_ticket_question(question):
        steps.append(
            PlanStep(
                specialist="integrations",
                goal=(
                    "Explain default policy: findings comment on the executed "
                    "story (no auto analysis ticket unless user authorized "
                    "create-on-fail); search Jira and fetch comments"
                ),
                need_tools=True,
            )
        )
        if pack.get("has_session") or any(t in q for t in ("sla", "fail", "p95", "why")):
            steps.append(
                PlanStep(
                    specialist="evidence_trends",
                    goal="Summarize why SLA/smoke failed from session and run history",
                    need_tools=True,
                )
            )
        return SupervisorPlan(
            steps=steps[:MAX_SIMPLE_SPECIALISTS],
            synthesize=len(steps) > 1,
            reason="heuristic_analysis_ticket",
        )

    if any(
        tok in q
        for tok in (
            "jira",
            "scrum-",
            "list stor",
            "list issue",
            "ticket",
        )
    ):
        steps.append(
            PlanStep(
                specialist="integrations",
                goal="List or fetch the requested ALM/Jira information via product REST",
                need_tools=True,
            )
        )
    if any(
        tok in q
        for tok in (
            "smoke",
            "p95",
            "fail",
            "kpi",
            "sla",
            "why",
        )
    ) and pack.get("has_session"):
        steps.append(
            PlanStep(
                specialist="evidence_trends",
                goal="Explain smoke/KPI evidence from session and local history",
                need_tools=True,
            )
        )
    if any(
        tok in q
        for tok in (
            "k6",
            "script",
            "assertion",
            "think time",
            "pacing",
            "ir",
            "correlation",
            "parameter",
            "txn",
            "transaction",
        )
    ):
        steps.append(
            PlanStep(
                specialist="scripting",
                goal="Advise on scripting/IR/assertions from session artifacts",
                need_tools=False,
            )
        )
    if not steps:
        steps.append(
            PlanStep(
                specialist="knowledge_qa",
                goal="Answer as a personal PE assistant using knowledge/RAG and session if any",
                need_tools=True,
            )
        )
    return SupervisorPlan(
        steps=steps[:MAX_SIMPLE_SPECIALISTS],
        synthesize=len(steps) > 1,
        reason="heuristic_fallback",
    )


def _looks_simple(question: str) -> bool:
    return len((question or "").split()) < 24


def _supervisor_tool_catalog() -> str:
    return (
        "Specialists: knowledge_qa (PE concepts, RAG), "
        "evidence_trends (smoke/KPI + Confluence sync_confluence_trends), "
        "integrations (Jira REST list/get — not Confluence trends), "
        "scripting (k6/IR/assertions). "
        "For Confluence/trend analysis ALWAYS use evidence_trends "
        "(sync_confluence_trends → KPI table). Never stop at wiki page links. "
        "For list/show Jira stories use integrations. "
        "Do not schedule browser/k6 execute — those are pipeline intents."
    )


def _format_missing(missing: List[str]) -> str:
    if not missing:
        return ""
    return "**Missing / next step:** " + "; ".join(missing[:5])


def _format_citations(handoffs: List[HandoffResult]) -> str:
    cites = []
    seen = set()
    for h in handoffs:
        for c in h.citations:
            key = (c.source, c.ref)
            if key in seen:
                continue
            seen.add(key)
            cites.append(f"- `{c.source}` {c.ref}".rstrip())
    if not cites:
        return ""
    return "\n\n**Sources:**\n" + "\n".join(cites[:12])
