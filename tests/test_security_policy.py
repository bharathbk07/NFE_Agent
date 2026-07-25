"""Unit tests for src/security policy helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.security.fs_jail import FsJailError, assert_under_jail, safe_artifact_filename
from src.security.secrets import (
    credentials_placeholders,
    redact_network_request,
    redact_text_for_llm,
    substitute_credential_placeholders,
)
from src.security.step_policy import StepPolicyError, assert_step_allowed, filter_allowed_steps
from src.security.url_policy import UrlPolicyError, assert_url_allowed, is_url_allowed


def test_url_policy_blocks_file_and_metadata():
    with pytest.raises(UrlPolicyError):
        assert_url_allowed("file:///etc/passwd")
    with pytest.raises(UrlPolicyError):
        assert_url_allowed("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UrlPolicyError):
        assert_url_allowed("http://127.0.0.1/", allow_localhost=False, deny_private=True)


def test_url_policy_allows_public_https():
    assert is_url_allowed("https://httpbin.org/forms/post", deny_private=True)


def test_url_allowlist():
    with pytest.raises(UrlPolicyError):
        assert_url_allowed(
            "https://evil.example/",
            allowlist=["httpbin.org"],
            deny_private=False,
        )
    assert_url_allowed(
        "https://httpbin.org/get",
        allowlist=["httpbin.org"],
        deny_private=False,
    )


def test_credential_placeholders_and_substitute():
    creds = {"username": "Admin", "password": "s3cret"}
    ph = credentials_placeholders(creds)
    assert ph["password"] == "${cred:password}"
    assert "s3cret" not in json.dumps(ph)
    steps = substitute_credential_placeholders(
        [{"action": "fill", "selector": "#pwd", "value": "${cred:password}"}],
        creds,
    )
    assert steps[0]["value"] == "s3cret"


def test_redact_text_and_headers():
    assert "***REDACTED***" in redact_text_for_llm('password=hunter2 user=a')
    req = redact_network_request(
        {
            "headers": {"Authorization": "Bearer abcdefghijklmnop", "Accept": "json"},
            "post_data": {"password": "x", "username": "u"},
        }
    )
    assert req["headers"]["Authorization"] == "***REDACTED***"
    assert req["headers"]["Accept"] == "json"
    assert req["post_data"]["password"] == "***REDACTED***"


def test_fs_jail(tmp_path: Path):
    jail = tmp_path / "recordings"
    jail.mkdir()
    good = jail / "host.json"
    good.write_text("{}")
    assert assert_under_jail(good, jail) == good.resolve()
    outside = tmp_path / "secret.json"
    outside.write_text("{}")
    with pytest.raises(FsJailError):
        assert_under_jail(outside, jail)
    with pytest.raises(FsJailError):
        safe_artifact_filename("../etc/passwd.js")
    assert safe_artifact_filename("demo.js") == "demo.js"


def test_step_policy():
    with pytest.raises(StepPolicyError):
        assert_step_allowed({"action": "evaluate", "js": "1+1"})
    with pytest.raises(StepPolicyError):
        assert_step_allowed({"action": "navigate", "url": "file:///tmp/x"})
    ok = assert_step_allowed(
        {"action": "click", "selector": "#btn"},
    )
    assert ok["action"] == "click"
    filtered = filter_allowed_steps(
        [
            {"action": "click", "selector": "#a"},
            {"action": "hack", "selector": "#b"},
        ]
    )
    assert len(filtered) == 1
