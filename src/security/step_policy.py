"""Allowlist Playwright actions and validate navigate steps."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, MutableMapping

from src.exceptions import ErrorCode, NFESecurityError
from src.security.url_policy import UrlPolicyError, assert_url_allowed

ALLOWED_ACTIONS = frozenset(
    {
        "navigate",
        "click",
        "fill",
        "select",
        "wait",
        "wait_for_selector",
        "wait_for_load",
        # Watch-me overlay markers (not LLM-planned browser ops)
        "txn_start",
        "txn_end",
        "initial_navigation",
    }
)

_DANGEROUS_SELECTOR_PREFIXES = ("javascript:", "data:", "vbscript:")


class StepPolicyError(NFESecurityError):
    """Raised when a planned/executed step violates policy."""

    default_code = ErrorCode.STEP_DENIED
    default_user_message = "This browser step was blocked by NFE security policy."

    def __init__(self, message: str = "", **kwargs: object) -> None:
        kwargs.setdefault("code", ErrorCode.STEP_DENIED)  # type: ignore[arg-type]
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


def assert_step_allowed(step: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate one Playwright step; return a shallow copy when allowed.

    Raises:
        StepPolicyError: For unknown actions, dangerous selectors, or bad URLs.
    """
    if not isinstance(step, Mapping):
        raise StepPolicyError("Step must be a mapping")
    action = str(step.get("action") or "").strip().lower()
    if not action:
        raise StepPolicyError("Step missing action")
    if action not in ALLOWED_ACTIONS:
        raise StepPolicyError(f"Action not allowed: {action}")

    selector = step.get("selector")
    if isinstance(selector, str):
        sel = selector.strip().lower()
        if any(sel.startswith(p) for p in _DANGEROUS_SELECTOR_PREFIXES):
            raise StepPolicyError(f"Dangerous selector blocked: {selector}")

    if action in ("navigate", "initial_navigation"):
        url = step.get("url")
        if url:
            try:
                assert_url_allowed(str(url))
            except UrlPolicyError as exc:
                raise StepPolicyError(str(exc)) from exc

    # Never allow model-supplied evaluate payloads if present
    if "evaluate" in step or "expression" in step or "js" in step:
        raise StepPolicyError("Custom page.evaluate / JS from steps is not allowed")

    return dict(step)


def filter_allowed_steps(steps: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only policy-compliant steps; drop or raise for navigate failures.

    Unknown actions are dropped. Navigate URL failures raise so callers fail closed.
    """
    out: List[Dict[str, Any]] = []
    for step in steps or []:
        action = str((step or {}).get("action") or "").strip().lower()
        try:
            out.append(assert_step_allowed(step))
        except StepPolicyError:
            if action == "navigate":
                raise
            continue
    return out
