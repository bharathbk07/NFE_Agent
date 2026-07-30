"""Tests for PE Agent OS core (Hands, Skills, approval, heartbeat, eval)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch


def test_list_jira_fast_path():
    from src.agents.runtime.assist_fastpath import (
        is_list_jira_request,
        try_assist_fast_path,
        wants_mcp,
    )

    assert is_list_jira_request("list all jira story")
    assert is_list_jira_request("show jira stories")
    assert is_list_jira_request("use MCP server to list all the jirs story/issues")
    assert wants_mcp("use MCP server to list jira issues")
    assert not wants_mcp("list jira stories")

    async def _run():
        with patch(
            "src.tools.pe_assistant_tools._list_jira_stories_impl",
            return_value=json.dumps(
                {
                    "count": 1,
                    "markdown": "* **SCRUM-1** — Create Claim",
                    "keys": ["SCRUM-1"],
                }
            ),
        ):
            return await try_assist_fast_path("list all jira story")

    out = asyncio.run(_run())
    assert out is not None
    assert "SCRUM-1" in out["answer"]
    assert out["tool_calls"] == ["list_jira_stories"]


def test_mcp_list_request_skips_rest(monkeypatch):
    from src.agents.runtime import assist_fastpath as fp

    async def fake_mcp(authorizations=None):
        return {
            "answer": "### Jira via MCP\n* SCRUM-9",
            "tool_calls": ["mcp:atlassian_search"],
            "pending_action": None,
            "agent_authorizations": [],
            "context_sources": ["mcp"],
            "plan": {"reason": "fastpath_list_jira_mcp", "ok": True},
        }

    monkeypatch.setattr(fp, "_via_mcp", fake_mcp)

    async def boom(*_a, **_k):
        raise AssertionError("REST must not run when MCP requested")

    monkeypatch.setattr(fp, "_via_rest", boom)
    out = asyncio.run(
        fp.try_assist_fast_path("use MCP server to list all the jira story/issues")
    )
    assert out is not None
    assert "SCRUM-9" in out["answer"]
    assert out["plan"]["ok"] is True


def test_404_is_retriable_for_failover():
    from src.utils.model_router import _is_retriable_llm_error

    assert _is_retriable_llm_error(Exception("HTTP/1.1 404 Not Found"))
    assert _is_retriable_llm_error(Exception("generator didn't stop after throw()"))


def test_needs_confirmation_skips_when_authorized():
    from src.agents.runtime.exec_approval import needs_confirmation
    from src.agents.runtime.hands_registry import HandSpec, RiskTier

    spec = HandSpec(
        name="execute_jira_story",
        description="x",
        risk=RiskTier.EXECUTE,
        capability="jira",
        tool=None,
        requires_confirm_unless_authorized=True,
        auth_keys=["execute_story"],
    )
    assert needs_confirmation(spec, authorizations=[]) is True
    assert needs_confirmation(spec, authorizations=["execute_story"]) is False


def test_pending_action_roundtrip():
    from src.agents.runtime.exec_approval import PendingAction

    p = PendingAction(
        kind="confirm_hand",
        hand_name="execute_jira_story",
        args={"issue_key": "SCRUM-1"},
        ask="confirm?",
        auth_keys=["execute_story"],
    )
    restored = PendingAction.from_dict(p.to_dict())
    assert restored is not None
    assert restored.hand_name == "execute_jira_story"
    assert restored.args["issue_key"] == "SCRUM-1"


def test_skills_catalog_includes_core_playbooks():
    from src.agents.runtime.skills import catalog_text, list_skills, load_skill

    ids = {s.id for s in list_skills()}
    assert "run-story-with-analysis" in ids
    assert "trend-sla-rca" in ids
    assert "HEARTBEAT" in ids
    assert "run-story-with-analysis" in catalog_text()
    body = load_skill("publish-evidence")
    assert "comment_jira_issue" in body


def test_hands_registry_covers_create_run_analyze_publish():
    from src.agents.runtime.hands import build_default_hands

    reg = build_default_hands(state={})
    names = {s.name for s in reg.list_specs()}
    for required in (
        "load_skill",
        "list_jira_stories",
        "rank_jira_stories",
        "execute_jira_story",
        "run_local_k6_smoke",
        "request_watch_me",
        "list_recordings",
        "reuse_recording",
        "format_run_report",
        "get_run_trends",
        "sync_confluence_trends",
        "comment_jira_issue",
        "create_jira_issue",
        "search_knowledge",
    ):
        assert required in names, required
    assert "execute" in reg.catalog_text()


def test_eval_suite_registry_coverage():
    from src.agents.runtime.eval_cases import score_registry_coverage
    from src.agents.runtime.hands import build_default_hands
    from src.agents.runtime.skills import list_skills

    hands = [s.name for s in build_default_hands(state={}).list_specs()]
    skills = [s.id for s in list_skills()]
    result = score_registry_coverage(hands, skills)
    assert result["ok"], result["failures"]


def test_create_issue_hand_blocked_when_flag_off(monkeypatch):
    from config import settings as settings_mod
    from src.agents.runtime.hands import build_default_hands

    monkeypatch.setattr(settings_mod.settings, "NFE_JIRA_CREATE_ENABLED", False)
    reg = build_default_hands(state={})
    tool = next(t for t in reg.tools() if getattr(t, "name", "") == "create_jira_issue")
    out = asyncio.run(
        tool.ainvoke(
            {
                "summary": "SLA fail",
                "description": "p95 blew",
                "acceptance_criteria": "p95 < 2s",
                "parent_key": "SCRUM-1",
            }
        )
    )
    data = json.loads(out)
    assert "error" in data
    assert "draft" in data


def test_heartbeat_disabled_returns_ok(monkeypatch):
    from config import settings as settings_mod
    from src.agents.runtime.heartbeat import HEARTBEAT_OK, run_heartbeat_once

    monkeypatch.setattr(settings_mod.settings, "NFE_HEARTBEAT_ENABLED", False)
    out = asyncio.run(run_heartbeat_once())
    assert out["message"] == HEARTBEAT_OK
    assert out["status"] == "disabled"


def test_heartbeat_proposes_when_stories_present(monkeypatch):
    from config import settings as settings_mod
    from src.agents.runtime.heartbeat import run_heartbeat_once

    monkeypatch.setattr(settings_mod.settings, "NFE_HEARTBEAT_ENABLED", True)
    with (
        patch(
            "src.agents.runtime.heartbeat._list_eligible_stories",
            return_value={"keys": ["SCRUM-9"], "markdown": "* SCRUM-9"},
        ),
        patch(
            "src.agents.runtime.heartbeat._unfinished_jobs_hint",
            return_value=[],
        ),
        patch("src.agents.runtime.heartbeat.append_note"),
        patch(
            "src.agents.runtime.heartbeat.load_skill",
            return_value="# HEARTBEAT\n",
        ),
    ):
        out = asyncio.run(run_heartbeat_once())
    assert out["status"] == "propose"
    assert "SCRUM-9" in out["message"]
    assert out["executed"] is False


def test_memory_append_and_read(tmp_path, monkeypatch):
    from src.agents.runtime import memory as mem

    monkeypatch.setattr(mem, "_memory_dir", lambda: tmp_path)
    mem.append_note("t1", "hello lane", kind="turn")
    ctx = mem.notes_as_context("t1")
    assert "hello lane" in ctx


def test_lane_serializes():
    from src.agents.runtime.lane import SessionLaneQueue

    async def _run() -> list[int]:
        q = SessionLaneQueue(global_limit=2)
        order: list[int] = []

        async def worker(n: int) -> None:
            async with q.acquire("same"):
                order.append(n)
                await asyncio.sleep(0.01)
                order.append(n + 10)

        await asyncio.gather(worker(1), worker(2))
        return order

    order = asyncio.run(_run())
    # Second worker cannot interleave mid-critical section
    assert order in ([1, 11, 2, 12], [2, 12, 1, 11])


def test_run_pe_agent_falls_back_when_disabled(monkeypatch):
    from config import settings as settings_mod
    from src.agents.runtime.pe_agent import run_pe_agent

    async def fake_run(state, question):
        return {"answer": "legacy", "tool_calls": [], "plan": {"reason": "fallback"}}

    monkeypatch.setattr(settings_mod.settings, "NFE_PE_AGENT_ENABLED", False)
    with patch("src.agents.runtime.supervisor.PESupervisor") as Super:
        Super.return_value.run = fake_run
        out = asyncio.run(run_pe_agent({}, "list stories"))
    assert out["answer"] == "legacy"
