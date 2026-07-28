"""Security policy helpers for the NFE Agent workflow.

Enforcement points: Playwright navigation/steps, LLM credential prompts,
artifact/recording path jails, and capture/log redaction.
"""

from src.security.fs_jail import (
    FsJailError,
    assert_under_jail,
    safe_artifact_filename,
)
from src.security.secrets import (
    credentials_for_storage,
    credentials_placeholders,
    env_name_for_credential,
    is_redacted_secret,
    redact_network_request,
    redact_run_records,
    redact_step,
    redact_text_for_llm,
    substitute_credential_placeholders,
)
from src.security.step_policy import (
    ALLOWED_ACTIONS,
    StepPolicyError,
    assert_step_allowed,
    filter_allowed_steps,
)
from src.security.url_policy import UrlPolicyError, assert_url_allowed, is_url_allowed

__all__ = [
    "ALLOWED_ACTIONS",
    "FsJailError",
    "StepPolicyError",
    "UrlPolicyError",
    "assert_step_allowed",
    "assert_under_jail",
    "assert_url_allowed",
    "credentials_for_storage",
    "credentials_placeholders",
    "env_name_for_credential",
    "filter_allowed_steps",
    "is_redacted_secret",
    "is_url_allowed",
    "redact_network_request",
    "redact_run_records",
    "redact_step",
    "redact_text_for_llm",
    "safe_artifact_filename",
    "substitute_credential_placeholders",
]
