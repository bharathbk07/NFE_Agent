"""Tests for Confluence trend sync + Jira assist list reliability."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

from src.utils.perf_trend import (
    filter_kpi_rows,
    parse_trend_filters,
    wants_confluence_evidence,
    wants_tool_refresh,
    wants_trend_question,
)


def test_wants_confluence_evidence_typos_and_phrases():
    assert wants_confluence_evidence("reterive the cofnluecne data")
    assert wants_confluence_evidence("retrieve the confluence data then")
    assert wants_confluence_evidence("where is the trend analysis based on that confluence data")
    assert wants_tool_refresh("pull from confluence")
    assert wants_trend_question("can u do trend ananlysis for that story")


def test_parse_trend_filters_exclude_smoke_and_vus():
    f = parse_trend_filters(
        "do not consider smoke test results; use user 10 for trend analysis"
    )
    assert f["exclude_smoke"] is True
    assert f["min_vus"] == 10


def test_filter_kpi_rows_exclude_smoke():
    rows = [
        {"run_id": "a", "source": "session_smoke", "p95_ms": 1},
        {"run_id": "b", "source": "confluence_sync", "workload_source": "jira_story", "vus": 10, "p95_ms": 2},
        {"run_id": "c", "summary": "deferred_to_jira_workload_run", "p95_ms": 3},
    ]
    kept, notes = filter_kpi_rows(rows, exclude_smoke=True, min_vus=10)
    assert len(kept) == 1
    assert kept[0]["run_id"] == "b"
    assert notes


def test_confluence_adapter_calls_correct_sync():
    from src.integrations.adapters.confluence_evidence import ConfluenceEvidenceAdapter

    with patch(
        "src.utils.perf_evidence.sync_confluence_and_build_report",
        return_value={
            "trend_markdown": "### Trend\n| a |",
            "synced_count": 2,
            "kpis": [{"confluence_url": "https://example/wiki/1"}],
            "notes": [],
        },
    ) as mocked:
        bundle = ConfluenceEvidenceAdapter().sync(
            "retrieve confluence trends",
            app="demo.app",
            flow="create-claim",
            state={"target_url": "https://demo.app"},
        )
    mocked.assert_called_once()
    kwargs = mocked.call_args.kwargs
    assert kwargs["app"] == "demo.app"
    assert kwargs["flow"] == "create-claim"
    assert kwargs["force"] is True
    assert "Trend" in bundle.markdown


def test_sync_confluence_trends_tool_builds_report():
    from src.tools.pe_assistant_tools import _sync_confluence_trends_impl

    fake_remote = [
        {
            "run_id": "confluence_1",
            "source": "confluence_sync",
            "p95_ms": 1200,
            "fail_rate": 0.1,
            "vus": 10,
            "confluence_url": "https://x/wiki/1",
            "flow_synced": "default",
        }
    ]
    with (
        patch(
            "src.utils.perf_evidence.ConfluenceEvidenceSource.sync",
            return_value=fake_remote,
        ),
        patch(
            "src.utils.perf_evidence.list_run_history",
            return_value=fake_remote,
        ),
    ):
        raw = _sync_confluence_trends_impl(
            question="trend from confluence exclude smoke user 10",
            app="opensource-demo.orangehrmlive.com",
            flow="create-claim",
            exclude_smoke=True,
            min_vus=10,
        )
    data = json.loads(raw)
    assert data.get("synced_count") == 1
    assert "Trend report" in (data.get("trend_markdown") or data.get("markdown") or "")
    assert data.get("source") == "tool:sync_confluence_trends"


def test_list_jira_assist_fallback_markdown():
    from src.integrations.jira.client import JiraIssue
    from src.tools.pe_assistant_tools import _list_jira_stories_impl

    board = [
        JiraIssue(
            key="SCRUM-1",
            summary="Create Claim",
            status="In Progress",
            labels=[],
        )
    ]
    with (
        patch("src.nodes.jira_story._list_eligible_issues", return_value=[]),
        patch("src.nodes.jira_story._list_assist_board_issues", return_value=board),
    ):
        raw = _list_jira_stories_impl(assist_fallback=True)
    data = json.loads(raw)
    assert data["count"] == 1
    assert "SCRUM-1" in data["markdown"]
    assert "nfe-agent" in data["markdown"].lower() or "missing" in data["markdown"].lower()
    assert "{" not in data["markdown"][:20]


def test_evidence_trends_fast_path_confluence():
    from src.agents.specialists.evidence_trends import EvidenceTrendsAgent

    agent = EvidenceTrendsAgent()
    with patch(
        "src.agents.specialists.evidence_trends._sync_confluence_trends_impl",
        return_value=json.dumps(
            {
                "synced_count": 2,
                "trend_markdown": "### Trend report from Confluence sync (2 page(s) synced)\n\n| Run |",
                "kpis": [{"run_id": "x"}],
                "markdown": "### Trend report",
            }
        ),
    ):
        out = asyncio.run(
            agent.run(
                goal="trend",
                question="okay but where is the trend analysis report based on that confluence data",
                context_pack={
                    "app": "opensource-demo.orangehrmlive.com",
                    "flow": "create-claim",
                    "has_session": False,
                },
                need_tools=True,
            )
        )
    assert out.specialist == "evidence_trends"
    assert "Trend report" in out.answer_md
    assert "sync_confluence_trends" in out.tool_calls


def test_resolve_evidence_scope_from_story_question_and_knowledge():
    from src.utils.app_registry import resolve_evidence_scope

    app, flow, url = resolve_evidence_scope(
        question=(
            "can u do trend report for that user story. "
            "consider only that which has 10 users not smoke test. "
            "if rag has no data full data from confluence and update the rag"
        ),
        app="",
        flow="default",
        target_url="",
        state={
            "messages": [
                type(
                    "M",
                    (),
                    {
                        "content": (
                            "SCRUM-1 [NFE] Smoke test — Create Claim (10 VUs)"
                        )
                    },
                )()
            ]
        },
    )
    assert app == "opensource-demo.orangehrmlive.com"
    assert flow == "create-claim"
    assert "orangehrm" in (url or app)


def test_evidence_trends_fast_path_empty_app_resolves():
    from src.agents.specialists.evidence_trends import EvidenceTrendsAgent

    agent = EvidenceTrendsAgent()
    captured = {}

    def _fake_sync(**kwargs):
        captured.update(kwargs)
        return json.dumps(
            {
                "synced_count": 1,
                "app": kwargs.get("app"),
                "flow": kwargs.get("flow"),
                "trend_markdown": "### Trend report from Confluence sync\n\n| Run | p95 |",
                "kpis": [{"run_id": "x", "vus": 10}],
                "markdown": "### Trend report",
            }
        )

    with patch(
        "src.agents.specialists.evidence_trends._sync_confluence_trends_impl",
        side_effect=lambda **kw: _fake_sync(**kw),
    ):
        out = asyncio.run(
            agent.run(
                goal="trend report",
                question=(
                    "trend report for that user story, only 10 users not smoke, "
                    "from confluence create-claim"
                ),
                context_pack={"app": "", "flow": "default", "has_session": False},
                need_tools=True,
            )
        )
    assert out.specialist == "evidence_trends"
    assert "Trend report" in out.answer_md
    assert captured.get("app") == "opensource-demo.orangehrmlive.com"
    assert captured.get("flow") == "create-claim"


def test_supervisor_heuristic_routes_confluence_to_evidence():
    from src.agents.runtime.supervisor import _heuristic_plan

    plan = _heuristic_plan(
        "retrieve the confluence data then if there is no data available locally",
        {"has_session": False},
    )
    assert len(plan.steps) == 1
    assert plan.steps[0].specialist == "evidence_trends"


def test_kpis_from_confluence_storage_extracts_vus():
    from src.utils.perf_evidence import _kpis_from_confluence_storage

    body = (
        "<td><p><strong>p95 latency</strong></p><p>1.2 s</p></td>"
        "<td><p><strong>Error rate</strong></p><p>1.5%</p></td>"
        "<td><p><strong>VUs (plan/max)</strong></p><p>10 / 10</p></td>"
        "PASSED jira_story"
    )
    kpis = _kpis_from_confluence_storage(body)
    assert kpis.get("vus") == 10
    assert kpis.get("p95_ms") == 1200.0
    assert kpis.get("workload_source") == "jira_story"
