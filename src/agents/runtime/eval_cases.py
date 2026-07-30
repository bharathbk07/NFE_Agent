"""Lightweight held-out eval cases for PE Agent Hand selection (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence, Set


@dataclass(frozen=True)
class EvalCase:
    id: str
    prompt: str
    expected_hands: Set[str]
    expected_skills: Set[str]


EVAL_CASES: List[EvalCase] = [
    EvalCase(
        id="story_run_create_on_fail",
        prompt=(
            "Work on the performance user story. If any issue in result, "
            "create a Jira issue for analysis."
        ),
        expected_hands={
            "rank_jira_stories",
            "execute_jira_story",
            "create_jira_issue",
            "format_run_report",
        },
        expected_skills={"run-story-with-analysis"},
    ),
    EvalCase(
        id="record_then_publish",
        prompt="Record create-claim on OrangeHRM, generate k6, smoke, publish Confluence.",
        expected_hands={
            "request_watch_me",
            "run_local_k6_smoke",
            "sync_confluence_trends",
        },
        expected_skills={"record-then-publish"},
    ),
    EvalCase(
        id="confluence_trends",
        prompt="Sync Confluence runs for create-claim, exclude smoke, VUs>=10, explain trend.",
        expected_hands={"sync_confluence_trends", "get_run_trends"},
        expected_skills={"trend-sla-rca"},
    ),
    EvalCase(
        id="reuse_heal",
        prompt="Reuse last recording, heal 401s, re-run, comment findings on SCRUM-1.",
        expected_hands={
            "list_recordings",
            "reuse_recording",
            "run_local_k6_smoke",
            "comment_jira_issue",
        },
        expected_skills={"build-script-from-recording", "publish-evidence"},
    ),
]


def score_registry_coverage(
    available_hands: Sequence[str],
    available_skills: Sequence[str],
    cases: Sequence[EvalCase] | None = None,
) -> dict:
    """Pass if every expected Hand/Skill exists in the installed catalogs."""
    hands = set(available_hands)
    skills = set(available_skills)
    cases = list(cases or EVAL_CASES)
    failures = []
    for case in cases:
        missing_h = sorted(case.expected_hands - hands)
        missing_s = sorted(case.expected_skills - skills)
        if missing_h or missing_s:
            failures.append(
                {
                    "id": case.id,
                    "missing_hands": missing_h,
                    "missing_skills": missing_s,
                }
            )
    return {
        "total": len(cases),
        "passed": len(cases) - len(failures),
        "failures": failures,
        "ok": not failures,
    }
