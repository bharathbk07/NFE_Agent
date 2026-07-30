"""Unit tests for chat-driven Jira intent and graph node helpers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from src.agents.intent_router import _heuristic_intent
from src.nodes.jira_story import extract_issue_key, run_jira_story, summarize_jira_result


def test_heuristic_jira_list_phrases():
    phrases = [
        "list all jira stroy",
        "list jira stories",
        "show jira issues",
        "list all jira stories?",
    ]
    for text in phrases:
        intent, confidence, _reason = _heuristic_intent(text, False)
        assert intent == "pe_assist", text
        assert confidence >= 0.9


def test_run_jira_story_list_only_does_not_auto_run():
    from src.integrations.jira.client import JiraIssue

    state = {"messages": [HumanMessage(content="list all jira stroy")]}
    only = JiraIssue(key="SCRUM-9", summary="Solo", status="To Do")
    with (
        patch(
            "src.nodes.jira_story._list_eligible_issues",
            return_value=[only],
        ),
        patch(
            "src.nodes.jira_story._prepare_and_run",
            new_callable=AsyncMock,
        ) as mocked,
    ):
        out = asyncio.run(run_jira_story(state))

    mocked.assert_not_awaited()
    content = out["messages"][0].content
    assert "SCRUM-9" in content
    assert "Eligible" in content or "work on" in content.lower()


def test_heuristic_jira_perf_phrases():
    """Only explicit execute commands short-circuit; NL mentions do not."""
    phrases = [
        "work on SCRUM-1",
        "work on jira story",
        "work on a jira story",
        "run jira",
        "process jira issue",
        "process issue SCRUM-2",
    ]
    for text in phrases:
        intent, confidence, _reason = _heuristic_intent(text, False)
        assert intent == "jira_perf", text
        assert confidence >= 0.9


def test_heuristic_does_not_trigger_jira_on_mentions():
    """Product-critical: talking about Jira must not auto-run the worker."""
    non_execute = [
        "what happened with the jira smoke?",
        "jira mentioned 50 VUs",
        "tell me about SCRUM-1",
        "did SCRUM-1 fail?",
        "the jira story has pacing of 5s",
        "compare this run to the jira ticket",
        "why did jira fail with 401",
    ]
    for text in non_execute:
        assert _heuristic_intent(text, True) is None, text
        assert _heuristic_intent(text, False) is None, text


def test_guard_downgrades_jira_question():
    from src.agents.intent_router import IntentDecision, _guard_pipeline_intents

    guarded = _guard_pipeline_intents(
        IntentDecision(
            intent="jira_perf",
            confidence=0.9,
            reason="llm said jira",
        ),
        "what about the jira smoke results?",
        True,
    )
    assert guarded.intent == "pe_assist"


def test_extract_issue_key():
    assert extract_issue_key("please work on SCRUM-1 now") == "SCRUM-1"
    assert extract_issue_key("no key here") is None
    assert extract_issue_key("keys ABC-9 and DEF-10") == "ABC-9"


def test_summarize_blocked_recording():
    msg = summarize_jira_result(
        {
            "issue": "SCRUM-1",
            "blocked": True,
            "reason": "recording_missing",
            "target_url": "https://example.com",
        }
    )
    assert "SCRUM-1" in msg
    assert "recording" in msg.lower()


def test_run_jira_story_with_key_mocked():
    state = {"messages": [HumanMessage(content="work on SCRUM-1")]}
    with patch(
        "src.nodes.jira_story._prepare_and_run",
        new_callable=AsyncMock,
        return_value={
            "jira_issue_key": "SCRUM-1",
            "messages": [AIMessage(content="Finished **SCRUM-1**.")],
        },
    ) as mocked:
        out = asyncio.run(run_jira_story(state))
    mocked.assert_awaited_once()
    assert mocked.await_args.args[0] == "SCRUM-1"
    assert out["jira_issue_key"] == "SCRUM-1"
    assert "SCRUM-1" in out["messages"][0].content


def test_run_jira_story_picks_latest_when_no_key():
    state = {"messages": [HumanMessage(content="work on jira story")]}
    fake_result = {"ok": True, "issue": "SCRUM-9", "smoke_ok": True}
    from src.integrations.jira.client import JiraIssue

    only = JiraIssue(key="SCRUM-9", summary="Solo", status="To Do")

    with (
        patch(
            "src.nodes.jira_story._list_eligible_issues",
            return_value=[only],
        ),
        patch(
            "src.nodes.jira_story._prepare_and_run",
            new_callable=AsyncMock,
            return_value={
                "jira_issue_key": "SCRUM-9",
                "messages": [AIMessage(content="ok SCRUM-9")],
            },
        ) as mocked,
    ):
        out = asyncio.run(run_jira_story(state))

    mocked.assert_awaited_once()
    assert mocked.await_args.args[0] == "SCRUM-9"
    assert out["jira_issue_key"] == "SCRUM-9"


def test_run_jira_story_asks_for_key_when_none():
    state = {"messages": [HumanMessage(content="work on jira story")]}
    with patch(
        "src.nodes.jira_story._list_eligible_issues",
        return_value=[],
    ):
        out = asyncio.run(run_jira_story(state))

    content = out["messages"][0].content.lower()
    assert "to do" in content or "scrum-1" in content


def test_assess_in_progress_comments_relevant():
    from src.nodes.jira_story import assess_in_progress_comments

    ok, why = assess_in_progress_comments(
        ["*NFE Agent* — recording not found"],
        description="",
    )
    assert ok is True
    assert "NFE" in why or "recording" in why.lower()


def test_assess_in_progress_comments_irrelevant():
    from src.nodes.jira_story import assess_in_progress_comments

    ok, _why = assess_in_progress_comments(
        ["Looks good to me", "Please review ASAP"],
        description="Just a random task",
    )
    assert ok is False


def test_format_candidate_list_mentions_keys():
    from src.integrations.jira.client import JiraIssue
    from src.nodes.jira_story import _format_candidate_list

    msg = _format_candidate_list(
        [
            JiraIssue(key="SCRUM-1", summary="A", status="To Do"),
            JiraIssue(key="SCRUM-2", summary="B", status="In Progress"),
        ]
    )
    assert "SCRUM-1" in msg and "SCRUM-2" in msg
    assert "Which one" in msg


def test_run_jira_story_asks_when_multiple():
    from src.integrations.jira.client import JiraIssue

    state = {"messages": [HumanMessage(content="work on jira story")]}
    issues = [
        JiraIssue(key="SCRUM-1", summary="One", status="To Do"),
        JiraIssue(key="SCRUM-2", summary="Two", status="In Progress"),
    ]
    with patch(
        "src.nodes.jira_story._list_eligible_issues",
        return_value=issues,
    ):
        out = asyncio.run(run_jira_story(state))
    assert "SCRUM-1" in out["messages"][0].content
    assert out.get("jira_candidate_keys") == ["SCRUM-1", "SCRUM-2"]
