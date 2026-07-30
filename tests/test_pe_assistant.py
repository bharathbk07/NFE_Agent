"""Golden / unit tests for PE multi-sub-agent assistant."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from src.agents.intent_router import _heuristic_intent, _promote_to_pe_assist, IntentDecision
from src.agents.runtime.contracts import Citation, HandoffResult, PlanStep, SupervisorPlan
from src.agents.runtime.context_pack import build_context_pack, slice_for_specialist
from src.agents.runtime.registry import clear_registry, ensure_default_agents, get_sub_agent
from src.tools.capability_tools import (
    clear_mcp_tool_cache,
    server_capability_index,
    servers_for_capabilities,
)
from src.utils.prompt_builder import PromptBuilder
from src.utils.prompt_loader import prompt_path


def test_prompt_nested_agents_path():
    p = prompt_path("agents/supervisor")
    assert p.name == "supervisor.txt"
    assert "agents" in str(p)


def test_prompt_builder_assembles_layers():
    pack = {
        "question": "what is correlation?",
        "app": "demo",
        "flow": "default",
        "has_session": False,
        "session_json": "{}",
        "knowledge_md": "Correlation links dynamic values.",
        "evidence_sources": ["knowledge"],
    }
    built = PromptBuilder().build(
        role="knowledge_qa",
        question="what is correlation?",
        context_pack=pack,
        tool_catalog="search_knowledge",
        goal="Explain correlation",
    )
    assert "correlation" in built.system.lower() or "Correlation" in built.system
    assert built.user
    assert built.evidence_meta.get("app") == "demo"


def test_contracts_roundtrip():
    plan = SupervisorPlan(
        steps=[PlanStep(specialist="knowledge_qa", goal="teach", need_tools=True)],
        synthesize=False,
        reason="test",
    )
    raw = plan.model_dump()
    assert SupervisorPlan.model_validate(raw).steps[0].specialist == "knowledge_qa"
    hr = HandoffResult(
        specialist="integrations",
        answer_md="ok",
        citations=[Citation(source="tool", ref="list_jira_stories")],
    )
    assert hr.citations[0].source == "tool"


def test_list_jira_routes_to_pe_assist():
    intent, conf, _ = _heuristic_intent("list all jira stroy", False)
    assert intent == "pe_assist"
    assert conf >= 0.9


def test_work_on_still_jira_perf():
    intent, conf, _ = _heuristic_intent("work on SCRUM-1", False)
    assert intent == "jira_perf"


def test_promote_conversation_to_pe_assist():
    d = IntentDecision(
        intent="conversation",
        confidence=0.7,
        reply="hi",
        reason="llm",
    )
    out = _promote_to_pe_assist(d, "what is think time in load testing?")
    assert out.intent == "pe_assist"


def test_greeting_stays_conversation():
    d = IntentDecision(intent="conversation", confidence=0.9, reply="Hi!", reason="hi")
    out = _promote_to_pe_assist(d, "hi")
    assert out.intent == "conversation"


def test_registry_loads_specialists():
    clear_registry()
    ensure_default_agents()
    assert get_sub_agent("knowledge_qa") is not None
    assert get_sub_agent("integrations") is not None
    assert get_sub_agent("evidence_trends") is not None
    assert get_sub_agent("scripting") is not None


def test_context_pack_and_slice():
    pack = build_context_pack({}, "what is pacing?", include_knowledge=False)
    assert "question" in pack
    sl = slice_for_specialist(pack, "knowledge_qa")
    assert "session_json" in sl


def test_mcp_capability_index_has_atlassian_fields():
    clear_mcp_tool_cache()
    idx = server_capability_index()
    # atlassian may be enabled in config
    if "atlassian" in idx:
        assert "jira" in idx["atlassian"]["capabilities"]
        assert idx["atlassian"]["read_only"] is True
        names = servers_for_capabilities(["jira", "alm"])
        assert "atlassian" in names


def test_integrations_list_fast_path():
    from src.agents.specialists.integrations import IntegrationsAgent
    from src.integrations.jira.client import JiraIssue

    agent = IntegrationsAgent()
    fake = [JiraIssue(key="SCRUM-1", summary="Claim", status="To Do")]
    with patch(
        "src.nodes.jira_story._list_eligible_issues",
        return_value=fake,
    ):
        out = asyncio.run(
            agent.run(
                goal="list stories",
                question="list all jira stories",
                context_pack={"app": "", "flow": "", "has_session": False},
                need_tools=True,
            )
        )
    assert out.specialist == "integrations"
    assert "SCRUM-1" in out.answer_md
    assert "list_jira_stories" in out.tool_calls


def test_supervisor_heuristic_plan_jira_list():
    from src.agents.runtime.supervisor import _heuristic_plan

    plan = _heuristic_plan("list jira stories", {"has_session": False})
    assert any(s.specialist == "integrations" for s in plan.steps)


def test_analysis_ticket_question_detection():
    from src.agents.specialists.integrations import wants_analysis_ticket_question

    assert wants_analysis_ticket_question(
        "did u created a jira issue for this for anlaysis why SLA got failed ?"
    )
    assert wants_analysis_ticket_question(
        "did you create an analysis ticket for the SLA failure?"
    )
    assert not wants_analysis_ticket_question("list jira stories")


def test_supervisor_heuristic_analysis_ticket():
    from src.agents.runtime.supervisor import _heuristic_plan

    plan = _heuristic_plan(
        "did you create a jira issue for analysis why SLA failed?",
        {"has_session": True},
    )
    assert plan.reason == "heuristic_analysis_ticket"
    assert plan.steps[0].specialist == "integrations"
    goal = plan.steps[0].goal.lower()
    assert (
        "create-on-fail" in goal
        or "comment" in goal
        or "does not create" in goal
        or "separate analysis" in goal
        or "default policy" in goal
    )
    assert any(s.specialist == "evidence_trends" for s in plan.steps)


def test_integrations_analysis_ticket_fast_path_no_list_stories():
    from src.agents.specialists.integrations import IntegrationsAgent

    agent = IntegrationsAgent()
    with (
        patch(
            "src.tools.pe_assistant_tools._search_jira_impl",
            return_value=json.dumps(
                {
                    "count": 1,
                    "keys": ["SCRUM-1"],
                    "markdown": "* **SCRUM-1** — Create Claim",
                }
            ),
        ),
        patch(
            "src.tools.pe_assistant_tools._get_jira_comments_impl",
            return_value=json.dumps(
                {
                    "key": "SCRUM-1",
                    "markdown": "### Comment 1\nSLA failed: p95",
                }
            ),
        ),
        patch(
            "src.tools.pe_assistant_tools._list_jira_stories_impl",
        ) as list_mock,
    ):
        out = asyncio.run(
            agent.run(
                goal="check analysis ticket",
                question="did u created a jira issue for this for anlaysis why SLA got failed ?",
                context_pack={
                    "app": "opensource-demo.orangehrmlive.com",
                    "flow": "create-claim",
                    "has_session": True,
                    "session_json": "{}",
                    "knowledge_md": "SCRUM-1 Create Claim",
                },
                need_tools=True,
            )
        )
    list_mock.assert_not_called()
    lowered = out.answer_md.lower()
    assert (
        "default policy" in lowered
        or "does not auto-create" in lowered
        or "does not create a separate analysis" in lowered
    )
    assert "search_jira" in out.tool_calls
    assert "get_jira_comments" in out.tool_calls


def test_assist_task_uses_reasoning_pool():
    from src.utils.model_router import REASONING_TASKS, TaskType

    assert TaskType.ASSIST in REASONING_TASKS
    assert TaskType.EXTRACTION not in REASONING_TASKS


def test_after_intent_routes_pe_assist():
    from src.nodes.routing import after_intent_router

    assert after_intent_router({"intent": "pe_assist"}) == "answer_analysis_question"
    assert after_intent_router({"intent": "analysis_qa"}) == "answer_analysis_question"
    assert after_intent_router({"intent": "jira_perf"}) == "run_jira_story"


def test_adapters_register():
    from src.integrations.adapters import (
        ensure_default_adapters,
        get_evidence_source,
        get_integration_adapter,
    )

    ensure_default_adapters()
    assert get_integration_adapter("jira_rest") is not None
    assert get_evidence_source("confluence") is not None
    assert get_evidence_source("monitoring") is not None
