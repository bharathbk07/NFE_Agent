"""Unit tests for the NFE exception hierarchy and helpers."""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from src.exceptions import (
    ErrorCode,
    NFEAuthError,
    NFEConfigError,
    NFEError,
    NFEIntegrationError,
    NFEPipelineError,
    NFESecurityError,
    NFEValidationError,
    is_hard_failure,
    node_failure_update,
    to_error_log_entry,
    to_user_message,
    wrap_unexpected,
)
from src.security.fs_jail import FsJailError
from src.security.step_policy import StepPolicyError
from src.security.url_policy import UrlPolicyError
from src.integrations.jira.client import JiraAPIError


def test_hierarchy_and_hard_failures():
    assert is_hard_failure(UrlPolicyError("denied"))
    assert is_hard_failure(StepPolicyError("bad step"))
    assert is_hard_failure(FsJailError("jail"))
    assert is_hard_failure(NFEConfigError("missing"))
    assert is_hard_failure(NFEAuthError("auth", code=ErrorCode.JIRA_AUTH))
    assert not is_hard_failure(NFEPipelineError("boom"))
    assert not is_hard_failure(NFEValidationError("bad"))
    assert not is_hard_failure(JiraAPIError("nf", status_code=404))
    assert is_hard_failure(
        NFEIntegrationError("x", code=ErrorCode.JIRA_AUTH)
    )


def test_security_subclass_codes():
    assert UrlPolicyError("x").code == ErrorCode.URL_DENIED
    assert StepPolicyError("x").code == ErrorCode.STEP_DENIED
    assert FsJailError("x").code == ErrorCode.FS_JAIL
    assert isinstance(UrlPolicyError("x"), NFESecurityError)
    assert isinstance(UrlPolicyError("x"), NFEError)


def test_to_user_message_redacts_secrets():
    msg = to_user_message(NFEError("login failed password=hunter2"))
    assert "hunter2" not in msg
    assert "***REDACTED***" in msg


def test_to_error_log_entry_includes_code():
    entry = to_error_log_entry(
        NFEPipelineError("k6 failed", code=ErrorCode.K6_SMOKE_FAILED)
    )
    assert entry.startswith(f"{ErrorCode.K6_SMOKE_FAILED}:")


def test_node_failure_update_shape(caplog):
    caplog.set_level(logging.WARNING)
    log = logging.getLogger("test_exc")
    exc = NFEValidationError("need url", user_message="Please provide a target URL.")
    update = node_failure_update(exc, logger=log, context="test")
    assert "error_log" in update
    assert update["error_log"][0].startswith(ErrorCode.VALIDATION)
    assert isinstance(update["messages"][0], AIMessage)
    assert "target URL" in update["messages"][0].content


def test_wrap_unexpected():
    wrapped = wrap_unexpected(RuntimeError("boom"))
    assert isinstance(wrapped, NFEPipelineError)
    assert wrapped.__cause__ is not None


def test_lazy_reexport():
    from src import exceptions as ex

    assert ex.UrlPolicyError is UrlPolicyError
    assert ex.JiraAPIError is JiraAPIError
