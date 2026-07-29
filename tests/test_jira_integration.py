"""Unit tests for Jira integration helpers (no live Jira)."""

from __future__ import annotations

import json

import pytest

from src.integrations.jira.client import JiraIssue
from src.integrations.jira.comments import comment_missing_recording, comment_results
from src.integrations.jira.labels import (
    LABEL_AGENT,
    LABEL_DONE,
    LABEL_RECORDING_READY,
    LABEL_RUNNING,
)
from src.integrations.jira.security import sanitize_comment
from src.integrations.jira.story_parser import parse_story_text
from src.integrations.jira.worker import should_process
from src.utils.k6_generator import _workload_options_js, emit_k6_from_ir


def test_adf_to_text():
    from src.integrations.jira.adf import adf_to_text

    doc = {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "NFE config"}],
            },
            {
                "type": "codeBlock",
                "attrs": {"language": "yaml"},
                "content": [
                    {
                        "type": "text",
                        "text": "recording: Create Claim\nvus: 10",
                    }
                ],
            },
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": "Hello"}],
            },
        ],
    }
    text = adf_to_text(doc)
    assert "## NFE config" in text
    assert "```yaml" in text
    assert "recording: Create Claim" in text
    assert "Hello" in text


def test_report_markup_to_adf_has_heading_and_bullets():
    from src.integrations.jira.adf import report_markup_to_adf

    adf = report_markup_to_adf(
        "## Test Summary\n* Status: *FAILED*\n* Target: `https://example.com`"
    )
    assert adf["type"] == "doc"
    types = [n["type"] for n in adf["content"]]
    assert "heading" in types
    assert "bulletList" in types


def test_parse_story_from_adf_style_text(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.jira.story_parser.assert_url_allowed",
        lambda u: u,
    )
    text = """
## Goal
Run smoke with 10 virtual users.

## NFE config

```yaml
recording: Create Claim
workload:
  vus: 10
  iterations: 20
thresholds:
  http_req_failed: ["rate<0.01"]
```
"""
    req = parse_story_text(description=text, validate_url=False)
    assert req.recording_hint == "Create Claim"
    assert req.workload.get("vus") == 10
    assert req.thresholds.get("http_req_failed")
    assert not req.errors or any("target_url" not in e for e in req.errors)


def test_sanitize_comment_redacts_password():
    body = sanitize_comment("password=hunter2 ok")
    assert "hunter2" not in body
    assert "***REDACTED***" in body


def test_should_process_label_gate():
    issue = JiraIssue(key="P-1", labels=[])
    ok, reason = should_process(issue)
    assert ok is False
    assert "nfe-agent" in reason or "routing" in reason

    issue2 = JiraIssue(key="P-1", labels=[LABEL_AGENT])
    assert should_process(issue2)[0] is True

    # Trailing comma on label (common Jira paste mistake)
    issue_comma = JiraIssue(key="P-1", labels=["nfe-agent,"])
    assert should_process(issue_comma)[0] is True

    issue3 = JiraIssue(key="P-1", labels=[LABEL_AGENT, LABEL_RUNNING])
    assert should_process(issue3)[0] is False

    issue4 = JiraIssue(key="P-1", labels=[LABEL_AGENT, LABEL_DONE])
    assert should_process(issue4)[0] is False

    issue5 = JiraIssue(
        key="P-1", labels=[LABEL_AGENT, LABEL_DONE, LABEL_RECORDING_READY]
    )
    assert should_process(issue5)[0] is True


def test_should_process_issue_types(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.jira.jql.settings.NFE_JIRA_ISSUE_TYPES",
        "Story,Issue",
    )
    ok_issue = JiraIssue(key="P-1", labels=[LABEL_AGENT], issue_type="Issue")
    assert should_process(ok_issue)[0] is True
    bad = JiraIssue(key="P-1", labels=[LABEL_AGENT], issue_type="Epic")
    ok, reason = should_process(bad)
    assert ok is False
    assert "issuetype" in reason


def test_build_poll_jql_includes_types(monkeypatch):
    from src.integrations.jira.jql import build_default_poll_jql

    monkeypatch.setattr(
        "src.integrations.jira.jql.settings.NFE_JIRA_ISSUE_TYPES",
        "Story, Issue",
    )
    monkeypatch.setattr(
        "src.integrations.jira.jql.settings.NFE_JIRA_LABEL",
        "nfe-agent",
    )
    monkeypatch.setattr(
        "src.integrations.jira.jql.settings.NFE_JIRA_STATUSES",
        "To Do,In Progress",
    )
    jql = build_default_poll_jql()
    assert 'labels = "nfe-agent"' in jql
    assert 'issuetype in ("Story", "Issue")' in jql
    assert 'status in ("To Do", "In Progress")' in jql


def test_comment_results_rich_sections():
    from src.integrations.jira.comments import comment_results

    body = comment_results(
        issue_key="SCRUM-1",
        target_url="https://example.com",
        smoke_ok=False,
        smoke_summary="failed (exit 1)",
        story_summary="Create Claim smoke",
        failed_urls=["GET https://example.com/x status=401"],
        failed_checks=["status is 2xx"],
        status_counts={"401": 3, "200": 10},
        transactions=[{"name": "Login"}],
        exit_code=1,
    )
    assert "Test Report" in body
    assert "Test Summary" in body
    assert "## Test Summary" in body or "Test Summary" in body
    assert "Statistics" in body
    assert "Failed Requests" in body
    assert "401" in body
    assert "Create Claim smoke" in body


def test_parse_story_yaml_fence(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.jira.story_parser.assert_url_allowed",
        lambda u: u,
    )
    text = """
Some AC text.

```yaml
target_url: https://httpbin.org/forms/post
recording: httpbin.org
workload:
  vus: 5
  iterations: 10
thresholds:
  http_req_failed: ["rate<0.01"]
```
"""
    req = parse_story_text(description=text, validate_url=True)
    assert req.target_url.startswith("https://httpbin.org")
    assert req.recording_hint == "httpbin.org"
    assert req.workload.get("vus") == 5
    assert not req.errors


def test_comment_templates_sanitized():
    msg = comment_missing_recording(target_url="https://example.com", host_hint="example.com")
    assert "nfe-recording-ready" in msg
    msg2 = comment_results(
        issue_key="PERF-9",
        target_url="https://example.com",
        smoke_ok=True,
        smoke_summary="password=should-redact",
        transactions=[{"name": "Login"}],
    )
    assert "PERF-9" in msg2
    assert "should-redact" not in msg2


def test_workload_options_in_emit(monkeypatch):
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_ON_FAIL",
        True,
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_DELAY",
        "10s",
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_FAIL_RATE",
        0.60,
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_P99_MS",
        30000,
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_CHECKS_MIN",
        0.40,
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_SLA_ABORT_ON_FAIL",
        False,
    )
    ir = {
        "version": 1,
        "target_url": "https://httpbin.org/",
        "vars": [],
        "correlations": [],
        "transactions": [
            {
                "name": "T1",
                "think_time_s": 0,
                "requests": [
                    {
                        "method": "GET",
                        "url": "https://httpbin.org/get",
                        "headers": {},
                        "body": None,
                        "expected_statuses": [200],
                    }
                ],
            }
        ],
        "workload": {"vus": 10, "iterations": 20, "maxDuration": "1m"},
    }
    opts = _workload_options_js(ir, browser=False)
    assert "vus: CONFIG.workload.vus" in opts
    assert "iterations: CONFIG.workload.iterations" in opts
    assert "thresholds: CONFIG.thresholds" in opts
    script = emit_k6_from_ir(ir)
    assert "USER CONFIG" in script
    assert '"vus": 10' in script
    assert '"iterations": 20' in script
    assert "CONFIG.workload.vus" in script
    assert "shared-iterations" in script
    assert "abortOnFail" in script
    assert "rate<0.6" in script


def test_catastrophic_abort_not_on_tight_sla(monkeypatch):
    """SLA rate<0.01 fails at end; only rate<0.6 (and peers) carry abortOnFail."""
    from src.utils.k6_generator import _inject_catastrophic_abort_thresholds

    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_FAIL_RATE", 0.60
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_P99_MS", 30000
    )
    monkeypatch.setattr(
        "config.settings.settings.NFE_K6_ABORT_CHECKS_MIN", 0.40
    )
    base = {
        "http_req_failed": [{"threshold": "rate<0.01"}],
        "http_req_duration": [{"threshold": "p(95)<2000"}],
        "checks": [{"threshold": "rate>0.99"}],
    }
    out = _inject_catastrophic_abort_thresholds(base, abort_delay="10s")
    fail_rules = out["http_req_failed"]
    sla = next(r for r in fail_rules if r.get("threshold") == "rate<0.01")
    abort = next(r for r in fail_rules if r.get("threshold") == "rate<0.6")
    assert sla.get("abortOnFail") is not True
    assert abort.get("abortOnFail") is True
    assert abort.get("delayAbortEval") == "10s"


def test_jira_api_error_message_for_404():
    from src.integrations.jira.client import JiraAPIError, _raise_for_status
    import httpx

    req = httpx.Request("GET", "https://example.atlassian.net/rest/api/3/issue/X-1")
    resp = httpx.Response(
        404,
        request=req,
        json={
            "errorMessages": [
                "Issue does not exist or you do not have permission to see it."
            ]
        },
    )
    with pytest.raises(JiraAPIError) as ei:
        _raise_for_status(resp, context="GET issue X-1", issue_key="X-1")
    msg = str(ei.value)
    assert "404" in msg
    assert "X-1" in msg
