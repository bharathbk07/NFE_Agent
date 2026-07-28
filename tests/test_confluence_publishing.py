"""Unit tests for Confluence publish gate, titles, and report bodies."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.confluence.publisher import (
    explain_confluence_skip,
    is_dominant_4xx_script_failure,
    resolve_flow_name,
    should_publish_to_confluence,
)
from src.integrations.confluence.report import (
    build_run_storage_body,
    resolve_run_status,
)
from src.integrations.confluence.security import sanitize_title
from src.integrations.jira.comments import comment_results


def test_sanitize_title_strips_unsafe():
    assert "[" not in sanitize_title("Create [Claim]/s")
    assert sanitize_title("") == "Untitled flow"


def test_resolve_flow_name_prefers_recording_stem():
    assert (
        resolve_flow_name(
            recording_file="artifacts/recordings/Create Claim.json",
            recording_hint="ignored",
            target_url="https://example.com/",
        )
        == "Create Claim"
    )


def test_should_publish_completed_with_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.JIRA_BASE_URL",
        "",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "bot@example.com",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "token",
    )

    summary = {
        "metrics": {
            "iterations": {"values": {"count": 2}},
            "http_req_duration": {
                "thresholds": {"p(95)<2000": {"ok": False}},
                "values": {"p(95)": 3000},
            },
        },
        "state": {"testRunDurationMs": 5000},
    }
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")

    smoke = {"ok": False, "skipped": False, "exit_code": 99, "summary": "failed"}
    assert should_publish_to_confluence(smoke, str(path)) is True


def test_should_publish_no_sla_completed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "bot@example.com",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "token",
    )
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "metrics": {"iterations": {"values": {"count": 2}}},
                "state": {},
            }
        ),
        encoding="utf-8",
    )
    assert (
        should_publish_to_confluence(
            {"ok": True, "skipped": False, "exit_code": 0}, str(path)
        )
        is True
    )


def test_should_not_publish_skipped(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    assert (
        should_publish_to_confluence(
            {"ok": False, "skipped": True, "summary": "k6 missing"}, ""
        )
        is False
    )


def test_should_not_publish_timeout_without_summary(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "bot@example.com",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "token",
    )
    assert (
        should_publish_to_confluence(
            {"ok": False, "skipped": False, "exit_code": -1, "summary": "timeout"},
            "",
        )
        is False
    )


def test_should_publish_despite_timeout_substring_in_stderr(tmp_path, monkeypatch):
    """summary.json with iterations wins over incidental 'timeout' in stderr."""
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "bot@example.com",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "token",
    )
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "metrics": {"iterations": {"values": {"count": 2}}},
                "state": {},
            }
        ),
        encoding="utf-8",
    )
    smoke = {
        "ok": True,
        "skipped": False,
        "exit_code": 0,
        "summary": "passed",
        "stderr": "http request timeout was 60s (config)",
    }
    assert should_publish_to_confluence(smoke, str(path)) is True


def test_explain_skip_missing_credentials(monkeypatch):
    from src.integrations.confluence.publisher import explain_confluence_skip

    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.JIRA_EMAIL",
        "",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.JIRA_API_TOKEN",
        "",
    )
    assert (
        explain_confluence_skip({"ok": True, "skipped": False, "exit_code": 0}, "")
        == "missing_confluence_credentials"
    )


def test_resolve_run_status_watcher_stopped():
    assert (
        resolve_run_status(smoke_ok=False, summary={}, aborted_by_watcher=True)
        == "COMPLETED — WATCHER STOPPED"
    )


def test_build_run_storage_includes_vus_tps(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "metrics": {
                    "iterations": {"values": {"count": 4}},
                    "http_reqs": {"values": {"count": 40, "rate": 12.5}},
                    "vus_max": {"values": {"value": 10}},
                },
                "state": {"testRunDurationMs": 3000},
            }
        ),
        encoding="utf-8",
    )
    body = build_run_storage_body(
        status="PASSED",
        flow_name="Create Claim",
        summary_json=str(summary),
        workload={"vus": 10, "iterations": 20, "executor": "shared-iterations"},
        workload_source="jira_story",
    )
    assert "VUs (plan/max)" in body
    assert "TPS" in body
    assert "12.5" in body
    assert "jira_story" in body
    assert "vus=10" in body
    assert "ac:name=\"status\"" in body
    assert "1. KPIs" in body


def test_comment_results_includes_vus_tps(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "metrics": {
                    "iterations": {"values": {"count": 4}},
                    "http_reqs": {"values": {"count": 40, "rate": 8.25}},
                    "vus_max": {"values": {"max": 5}},
                },
                "state": {},
            }
        ),
        encoding="utf-8",
    )
    body = comment_results(
        issue_key="SCRUM-9",
        target_url="https://example.com",
        smoke_ok=True,
        workload={"vus": 5, "iterations": 10},
        workload_source="jira_story",
        summary_json=str(summary),
        exit_code=0,
    )
    assert "Virtual users (planned): `5`" in body
    assert "TPS / HTTP req rate" in body
    assert "8.25" in body
    assert "jira_story" in body



def test_should_not_publish_without_space(monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    assert (
        should_publish_to_confluence(
            {"ok": True, "skipped": False, "exit_code": 0}, ""
        )
        is False
    )


def test_resolve_run_status_sla_failed():
    summary = {
        "metrics": {
            "http_req_failed": {
                "thresholds": {"rate<0.01": {"ok": False}},
                "values": {},
            }
        }
    }
    assert resolve_run_status(smoke_ok=True, summary=summary) == "COMPLETED — SLA FAILED"


def test_build_run_storage_includes_status():
    body = build_run_storage_body(
        status="COMPLETED — SLA FAILED",
        flow_name="Create Claim",
        target_url="https://example.com",
        failed_urls=["GET /x status=401"],
    )
    assert "COMPLETED — SLA FAILED" in body
    assert "Create Claim" in body
    assert "Failed request list" in body
    assert "GET /x status=401" in body


def test_should_not_publish_dominant_4xx(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.NFE_CONFLUENCE_PUBLISH",
        True,
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_SPACE_KEY",
        "ENG",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_BASE_URL",
        "https://example.atlassian.net",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_EMAIL",
        "bot@example.com",
    )
    monkeypatch.setattr(
        "src.integrations.confluence.publisher.settings.CONFLUENCE_API_TOKEN",
        "token",
    )
    summary = {
        "metrics": {
            "iterations": {"values": {"count": 2}},
            "http_req_failed": {"values": {"rate": 0.8}},
        },
        "state": {"testRunDurationMs": 5000},
    }
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    smoke = {
        "ok": False,
        "skipped": False,
        "exit_code": 99,
        "summary": "failed",
        "status_counts": {"401": 12, "200": 2},
        "failed_urls": [
            "GET /a status=401",
            "POST /b status=404",
            "GET /c status=401",
        ],
    }
    assert is_dominant_4xx_script_failure(smoke, summary) is True
    assert explain_confluence_skip(smoke, str(path)) == "script_4xx_failures"
    assert should_publish_to_confluence(smoke, str(path)) is False


def test_build_run_storage_html_parity_from_points(tmp_path):
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "metrics": {
                    "iterations": {"values": {"count": 1}},
                    "http_reqs": {"values": {"count": 2, "rate": 1.0}},
                    "http_req_failed": {"values": {"rate": 0.5}},
                    "http_req_duration": {
                        "thresholds": {"p(95)<2000": {"ok": True}},
                        "values": {"p(95)": 100, "avg": 80, "max": 120},
                    },
                },
                "state": {"testRunDurationMs": 2000},
            }
        ),
        encoding="utf-8",
    )
    points = tmp_path / "k6-points.json"
    points.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_txn_duration",
                        "data": {
                            "value": 150.0,
                            "tags": {"txn": "Login"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_txn_fail",
                        "data": {"value": 0, "tags": {"txn": "Login"}},
                    }
                ),
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_req_duration",
                        "data": {
                            "value": 90.0,
                            "tags": {
                                "txn": "Login",
                                "method": "GET",
                                "url": "https://example.com/login",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_req_count",
                        "data": {
                            "value": 1,
                            "tags": {
                                "txn": "Login",
                                "method": "GET",
                                "url": "https://example.com/login",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_req_fail",
                        "data": {
                            "value": 1,
                            "tags": {
                                "txn": "Login",
                                "method": "POST",
                                "url": "https://example.com/api",
                                "status": "401",
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "Point",
                        "metric": "nfe_req_count",
                        "data": {
                            "value": 1,
                            "tags": {
                                "txn": "Login",
                                "method": "POST",
                                "url": "https://example.com/api",
                            },
                        },
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    body = build_run_storage_body(
        status="COMPLETED — CHECKS/SCRIPT ISSUES",
        flow_name="Login Flow",
        summary_json=str(summary),
        points_json=str(points),
        heal_notes=["rebound cookie"],
    )
    assert "Full transaction table" in body
    assert "Full request table" in body
    assert "Failed request list" in body
    assert "Test observation" in body
    assert "Login" in body
    assert "https://example.com/login" in body
    assert "401" in body
    assert "ac:name=\"status\"" in body
    assert "colour\">Red" in body or "colour\">Yellow" in body
    assert "rebound cookie" in body
    assert "PASS" in body or "FAIL" in body


def test_comment_results_why_failed_and_confluence():
    body = comment_results(
        issue_key="SCRUM-1",
        target_url="https://example.com",
        smoke_ok=False,
        smoke_summary="failed (exit 1)",
        failed_urls=["GET https://example.com/x status=401"],
        failed_checks=["not 4xx"],
        status_counts={"401": 2},
        exit_code=1,
        confluence_url="https://example.atlassian.net/wiki/spaces/ENG/pages/1",
    )
    assert "Why it failed" in body
    assert "401" in body
    assert "Confluence" in body
    assert "wiki/spaces/ENG" in body


def test_comment_results_mid_run_timeout():
    body = comment_results(
        issue_key="SCRUM-2",
        target_url="https://example.com",
        smoke_ok=False,
        smoke_summary="timeout",
        exit_code=-1,
        skipped=False,
    )
    assert "Why it failed" in body
    assert "stopped mid-way" in body.lower() or "timeout" in body.lower()


def test_publish_run_results_mocked(monkeypatch, tmp_path):
    from src.integrations.confluence import publisher as pub

    monkeypatch.setattr(pub.settings, "NFE_CONFLUENCE_PUBLISH", True)
    monkeypatch.setattr(pub.settings, "CONFLUENCE_SPACE_KEY", "ENG")
    monkeypatch.setattr(
        pub.settings, "CONFLUENCE_BASE_URL", "https://example.atlassian.net"
    )
    monkeypatch.setattr(pub.settings, "CONFLUENCE_EMAIL", "bot@example.com")
    monkeypatch.setattr(pub.settings, "CONFLUENCE_API_TOKEN", "token")
    monkeypatch.setattr(
        pub.settings, "CONFLUENCE_PARENT_TITLE", "Performance Testing and Engineering"
    )

    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"metrics": {"iterations": {"values": {"count": 2}}}, "state": {}}),
        encoding="utf-8",
    )
    script = tmp_path / "host.js"
    script.write_text("// k6", encoding="utf-8")
    html = tmp_path / "html-report.html"
    html.write_text("<html></html>", encoding="utf-8")

    pages = {}

    class FakeClient:
        space_key = "ENG"
        base_url = "https://example.atlassian.net"

        def page_url(self, page_id: str) -> str:
            return f"{self.base_url}/wiki/spaces/ENG/pages/{page_id}"

        def find_page_by_title(self, title, *, parent_id=None):
            key = (title, parent_id)
            return pages.get(key)

        def create_page(self, *, title, storage_body, parent_id=None):
            page = {
                "id": str(len(pages) + 1),
                "title": title,
                "version": {"number": 1},
            }
            pages[(title, parent_id)] = page
            return page

        def get_page(self, page_id, *, expand="version"):
            for page in pages.values():
                if str(page["id"]) == str(page_id):
                    return dict(page)
            return {"id": page_id, "title": "x", "version": {"number": 1}}

        def update_page(self, *, page_id, title, storage_body, version_number):
            return {
                "id": page_id,
                "title": title,
                "version": {"number": version_number + 1},
            }

        def upload_attachment(self, *, page_id, file_path, filename=None):
            return {"title": filename or Path(file_path).name}

    monkeypatch.setattr(pub, "ConfluenceClient", FakeClient)

    result = pub.publish_run_results(
        {
            "target_url": "https://example.com/login",
            "recording_file": str(tmp_path / "Create Claim.json"),
            "k6_path": str(script),
            "html_report": str(html),
            "summary_json": str(summary),
            "smoke_result": {
                "ok": True,
                "skipped": False,
                "exit_code": 0,
                "summary": "passed",
                "summary_json": str(summary),
            },
            "smoke_ok": True,
            "transactions": [{"name": "Login"}],
        }
    )
    assert result["published"] is True
    assert result["flow_name"] == "Create Claim"
    assert "run_url" in result
    assert result["attachments"]
