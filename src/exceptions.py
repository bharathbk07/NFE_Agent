"""Typed NFE exceptions with safe user/log surfaces (no secret leakage).

Hybrid handling convention:
- Hard (fail closed): ``NFESecurityError``, ``NFEConfigError``, ``NFEAuthError``
- Soft (error_log + AIMessage): ``NFEPipelineError``, ``NFEValidationError``,
  most ``NFEIntegrationError``; unexpected bugs log with stack then soft-fail
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Sequence

from langchain_core.messages import AIMessage

# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------


class ErrorCode:
    """Stable machine-readable codes for logs and tests."""

    UNKNOWN = "UNKNOWN"
    CONFIG_MISSING = "CONFIG_MISSING"
    CONFIG_INVALID = "CONFIG_INVALID"
    AUTH = "AUTH"
    URL_DENIED = "URL_DENIED"
    STEP_DENIED = "STEP_DENIED"
    FS_JAIL = "FS_JAIL"
    VALIDATION = "VALIDATION"
    PIPELINE = "PIPELINE"
    RECORDING_MISSING = "RECORDING_MISSING"
    K6_SMOKE_FAILED = "K6_SMOKE_FAILED"
    K6_SCRIPT_MISSING = "K6_SCRIPT_MISSING"
    INTEGRATION = "INTEGRATION"
    JIRA_AUTH = "JIRA_AUTH"
    JIRA_NOT_FOUND = "JIRA_NOT_FOUND"
    JIRA_API = "JIRA_API"
    LLM_PROVIDER = "LLM_PROVIDER"


# ---------------------------------------------------------------------------
# Hierarchy
# ---------------------------------------------------------------------------


class NFEError(Exception):
    """Base NFE exception with safe user-facing fields."""

    default_code: str = ErrorCode.UNKNOWN
    default_user_message: str = "Something went wrong in the NFE agent."

    def __init__(
        self,
        message: str = "",
        *,
        code: Optional[str] = None,
        user_message: Optional[str] = None,
        details: Optional[Mapping[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        msg = (message or user_message or self.default_user_message).strip()
        super().__init__(msg)
        self.code = (code or self.default_code).strip() or ErrorCode.UNKNOWN
        self.user_message = (user_message or msg or self.default_user_message).strip()
        self.details: Dict[str, Any] = dict(details or {})
        self.__cause__ = cause


class NFEConfigError(NFEError):
    """Missing or invalid configuration / environment."""

    default_code = ErrorCode.CONFIG_MISSING
    default_user_message = "NFE configuration is incomplete or invalid."


class NFEAuthError(NFEError):
    """Authentication or authorization failure (fail closed)."""

    default_code = ErrorCode.AUTH
    default_user_message = "Authentication failed. Check credentials and permissions."


class NFESecurityError(NFEError):
    """Security policy violation (URL / step / filesystem jail)."""

    default_code = ErrorCode.URL_DENIED
    default_user_message = "This action was blocked by NFE security policy."


class NFEValidationError(NFEError):
    """Invalid user input, story payload, or missing required fields."""

    default_code = ErrorCode.VALIDATION
    default_user_message = "The request is missing required information or is invalid."


class NFEPipelineError(NFEError):
    """Capture / analysis / k6 pipeline failure (soft-fail)."""

    default_code = ErrorCode.PIPELINE
    default_user_message = "The performance pipeline failed."


class NFEIntegrationError(NFEError):
    """External integration failure (Jira REST, MCP, providers)."""

    default_code = ErrorCode.INTEGRATION
    default_user_message = "An external service call failed."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HARD_TYPES = (NFESecurityError, NFEConfigError, NFEAuthError)


def is_hard_failure(exc: BaseException) -> bool:
    """True when the error must fail closed (security / config / auth)."""
    if isinstance(exc, _HARD_TYPES):
        return True
    if isinstance(exc, NFEIntegrationError) and exc.code in (
        ErrorCode.AUTH,
        ErrorCode.JIRA_AUTH,
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status in (401, 403) and type(exc).__name__ == "JiraAPIError":
        return True
    return False


def _redact(text: str) -> str:
    try:
        from src.security.secrets import redact_text_for_llm

        return redact_text_for_llm(text or "")
    except Exception:
        return text or ""


def to_user_message(exc: BaseException) -> str:
    """Safe chat/comment message — never a traceback; secrets redacted."""
    if isinstance(exc, NFEError):
        raw = exc.user_message or str(exc) or exc.default_user_message
    else:
        raw = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    # Strip accidental multi-line stacks if someone stuffed them in the message
    first = (raw or "").strip().splitlines()[0] if raw else ""
    return _redact(first)[:2000]


def to_error_log_entry(exc: BaseException) -> str:
    """Short ``CODE: message`` line for ``AgentState.error_log``."""
    if isinstance(exc, NFEError):
        code = exc.code
        msg = to_user_message(exc)
        return f"{code}: {msg}"
    return _redact(f"{type(exc).__name__}: {exc}")[:2000]


def log_exception(
    logger: logging.Logger,
    exc: BaseException,
    *,
    level: Optional[int] = None,
    context: str = "",
) -> None:
    """Log domain errors at warning; unexpected errors with stack traces."""
    prefix = f"{context}: " if context else ""
    if isinstance(exc, NFEError):
        lvl = level if level is not None else logging.WARNING
        logger.log(
            lvl,
            "%s[%s] %s",
            prefix,
            exc.code,
            to_user_message(exc),
            exc_info=(lvl >= logging.ERROR),
        )
        return
    # Unexpected
    if is_hard_failure(exc):
        logger.error("%s%s", prefix, to_user_message(exc), exc_info=True)
    else:
        logger.exception("%sUnexpected error: %s", prefix, to_user_message(exc))


def node_failure_update(
    exc: BaseException,
    *,
    extra: Optional[Mapping[str, Any]] = None,
    prior_error_log: Optional[Sequence[str]] = None,
    logger: Optional[logging.Logger] = None,
    context: str = "",
) -> Dict[str, Any]:
    """Build a LangGraph state update for a failed node (soft path)."""
    if logger is not None:
        log_exception(logger, exc, context=context)
    entry = to_error_log_entry(exc)
    log = list(prior_error_log or [])
    log.append(entry)
    updates: Dict[str, Any] = {
        "error_log": log,
        "messages": [AIMessage(content=to_user_message(exc))],
    }
    if extra:
        updates.update(dict(extra))
    return updates


def wrap_unexpected(
    exc: BaseException,
    *,
    code: str = ErrorCode.PIPELINE,
    user_message: str = "An unexpected error occurred in the NFE pipeline.",
) -> NFEPipelineError:
    """Wrap a bare exception as ``NFEPipelineError`` for soft-fail paths."""
    if isinstance(exc, NFEError):
        return exc  # type: ignore[return-value]
    return NFEPipelineError(
        str(exc) or user_message,
        code=code,
        user_message=user_message,
        cause=exc,
    )


# Lazy re-exports so ``from src.exceptions import UrlPolicyError`` works
# without import cycles at module load.
_LAZY_EXPORTS = {
    "UrlPolicyError": ("src.security.url_policy", "UrlPolicyError"),
    "StepPolicyError": ("src.security.step_policy", "StepPolicyError"),
    "FsJailError": ("src.security.fs_jail", "FsJailError"),
    "JiraAPIError": ("src.integrations.jira.client", "JiraAPIError"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        mod_name, attr = _LAZY_EXPORTS[name]
        import importlib

        mod = importlib.import_module(mod_name)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ErrorCode",
    "NFEError",
    "NFEConfigError",
    "NFEAuthError",
    "NFESecurityError",
    "NFEValidationError",
    "NFEPipelineError",
    "NFEIntegrationError",
    "is_hard_failure",
    "to_user_message",
    "to_error_log_entry",
    "log_exception",
    "node_failure_update",
    "wrap_unexpected",
    "UrlPolicyError",
    "StepPolicyError",
    "FsJailError",
    "JiraAPIError",
]
