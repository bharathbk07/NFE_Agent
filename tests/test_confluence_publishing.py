"""Unit tests for Confluence publish gate, titles, and report bodies."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.integrations.confluence.publisher import (
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


def test_should_not_publish_timeout(monkeypatch):
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
            {"ok": False, "skipped": False, "exit_code": -1, "summary": "timeout"},
            "",
        )
        is False
    )


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
    assert "Failed requests" in body


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
