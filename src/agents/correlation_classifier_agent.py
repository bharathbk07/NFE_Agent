"""
LLM-assisted parameter vs correlation classification after capture runs.

Uses deterministic evidence (fills, HTTP diffs, cookies) and optionally
recommends an extra run when the model is unsure.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Literal, Optional

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src.utils.model_router import TaskType, get_model_router
from src.utils.perf_test_classification import (
    is_placeholder_value,
    suggest_correlation_var_name,
)
from src.utils.prompt_loader import load_prompt_text

logger = logging.getLogger(__name__)

FillClass = Literal["parameter", "correlation", "uncertain"]
CookieConfidence = Literal["high", "medium", "low", "uncertain"]


class FillClassification(BaseModel):
    """An LLM classification of one UI fill as a parameter or correlation."""

    selector: str = ""
    value_preview: str = ""
    classification: FillClass = "uncertain"
    variable_name: str = ""
    reason: str = ""


class CookieRelationNote(BaseModel):
    """Correlation guidance for a cookie observed across capture runs."""

    cookie_name: str
    confidence: CookieConfidence = "uncertain"
    must_correlate: bool = False
    extract_hint: str = Field(
        default="",
        description="Where the cookie is set (e.g. Set-Cookie on login response)",
    )
    pass_to_hint: str = Field(
        default="",
        description="Where it is sent (e.g. Cookie header on /api/v2/...)",
    )
    note: str = Field(
        default="",
        description="Advice for the tester — especially when unsure",
    )


class CorrelationAdvice(BaseModel):
    """Structured classifier output combining fill and cookie recommendations."""

    fill_classifications: List[FillClassification] = Field(default_factory=list)
    cookie_notes: List[CookieRelationNote] = Field(default_factory=list)
    needs_extra_run: bool = False
    extra_run_reason: Optional[str] = None
    summary: str = ""


def _truncate(text: Any, max_len: int = 120) -> str:
    """Convert a value to bounded prompt-safe text.

    Args:
        text: Value to stringify; ``None`` becomes an empty string.
        max_len: Maximum returned character count.

    Returns:
        The full string or an ellipsis-truncated representation.
    """
    s = "" if text is None else str(text)
    if len(s) <= max_len:
        return s
    return s[: max_len - 3] + "..."


def _load_prompt() -> ChatPromptTemplate:
    """Load the correlation-classifier prompt from the repository.

    Returns:
        A chat prompt template for structured correlation classification.

    Raises:
        OSError: If the prompt file cannot be read.
    """
    return ChatPromptTemplate.from_template(
        load_prompt_text("correlation_classifier")
    )


def _fill_steps_evidence(user_steps: List[Any]) -> List[Dict[str, Any]]:
    """Select bounded fill and select evidence for the classifier prompt.

    Args:
        user_steps: Journey steps, with non-dictionary entries ignored.

    Returns:
        Up to 40 compact fill/select evidence dictionaries.
    """
    out = []
    for idx, step in enumerate(user_steps or []):
        if not isinstance(step, dict):
            continue
        if step.get("action") not in ("fill", "select"):
            continue
        out.append(
            {
                "step_index": idx,
                "action": step.get("action"),
                "selector": step.get("selector"),
                "value": _truncate(step.get("value"), 80),
                "sub_task": step.get("sub_task"),
            }
        )
    return out[:40]


def _cookie_pairs(
    run1: Dict[str, Any], run2: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Compare cookie jars from two journey runs by cookie name.

    Args:
        run1: First run record containing a ``cookies`` list.
        run2: Second run record containing a ``cookies`` list.

    Returns:
        Up to 40 cookie comparison rows with value previews and change flags.
    """
    c1 = {
        str(c.get("name")): str(c.get("value") or "")
        for c in (run1.get("cookies") or [])
        if isinstance(c, dict) and c.get("name")
    }
    c2 = {
        str(c.get("name")): str(c.get("value") or "")
        for c in (run2.get("cookies") or [])
        if isinstance(c, dict) and c.get("name")
    }
    # Use the union so cookies created or removed in only one run remain visible.
    names = sorted(set(c1) | set(c2))
    rows = []
    for name in names[:40]:
        v1 = c1.get(name, "")
        v2 = c2.get(name, "")
        rows.append(
            {
                "name": name,
                "run1_preview": _truncate(v1, 48),
                "run2_preview": _truncate(v2, 48),
                "changed": v1 != v2 and bool(v1 or v2),
            }
        )
    return rows


def _set_cookie_names_from_requests(requests: List[Dict[str, Any]]) -> List[str]:
    """Extract unique cookie names from captured Set-Cookie headers.

    Args:
        requests: Captured request dictionaries with response headers.

    Returns:
        Up to 30 case-insensitively unique cookie names.
    """
    names: List[str] = []
    seen = set()
    for req in requests or []:
        headers = req.get("response_headers") or {}
        for hk, hv in headers.items():
            if str(hk).lower() != "set-cookie":
                continue
            # This intentionally provides prompt evidence rather than full RFC
            # parsing; the first name=value segment is sufficient here.
            for part in str(hv).split(","):
                first = part.split(";")[0]
                if "=" in first:
                    n = first.split("=", 1)[0].strip()
                    if n and n.lower() not in seen:
                        seen.add(n.lower())
                        names.append(n)
    return names[:30]


def _dynamic_sample(correlations: List[Dict[str, Any]], limit: int = 25) -> List[Dict]:
    """Build a compact sample of dynamic-value differences.

    Args:
        correlations: Detected cross-run correlation dictionaries.
        limit: Maximum number of samples to return.

    Returns:
        Prompt-ready dictionaries containing bounded values and URLs.
    """
    sample = []
    for c in correlations or []:
        sample.append(
            {
                "key": c.get("key") or c.get("dynamic_name"),
                "location": c.get("location"),
                "run1": _truncate(c.get("run1_value"), 40),
                "run2": _truncate(c.get("run2_value"), 40),
                "url": _truncate(c.get("request_url"), 90),
            }
        )
        if len(sample) >= limit:
            break
    return sample


def _response_snippets(
    requests: List[Dict[str, Any]], limit: int = 8
) -> List[Dict[str, Any]]:
    """Select likely state-changing response snippets for LLM inspection.

    Args:
        requests: Captured network requests and response bodies.
        limit: Maximum number of snippets to return.

    Returns:
        Bounded response summaries for mutation or workflow-related requests.
    """
    hints = ("claim", "order", "submit", "create", "auth", "login", "session")
    snippets = []
    for req in requests or []:
        method = (req.get("method") or "").upper()
        url = (req.get("url") or "").lower()
        if method not in ("POST", "PUT", "PATCH") and not any(h in url for h in hints):
            continue
        body = req.get("response_body") or ""
        if not body:
            continue
        snippets.append(
            {
                "method": method,
                "url": _truncate(req.get("url"), 100),
                "step_index": req.get("step_index"),
                "status": req.get("status"),
                "body_preview": _truncate(body, 400),
            }
        )
        if len(snippets) >= limit:
            break
    return snippets


def _journey_summary(user_steps: List[Any], sub_tasks: Optional[List[Dict]] = None) -> str:
    """Summarize an NFE journey from phases or raw actions.

    Args:
        user_steps: Ordered browser journey steps.
        sub_tasks: Optional orchestrated journey phases.

    Returns:
        A bounded human-readable journey summary.
    """
    if sub_tasks:
        return " → ".join(
            str(t.get("name") or t.get("description") or "") for t in sub_tasks
        )[:500]
    actions = []
    for s in (user_steps or [])[:30]:
        if isinstance(s, dict):
            actions.append(str(s.get("action") or ""))
    return ", ".join(actions)[:400]


class CorrelationClassifierAgent:
    """Classifies parameters, correlations, and cookie handling with an LLM."""

    def __init__(self):
        """Configure the classifier with the shared failover model router."""
        self.router = get_model_router()

    async def classify(
        self,
        *,
        target_url: str,
        user_steps: List[Any],
        credentials: Dict[str, str],
        run1: Dict[str, Any],
        run2: Dict[str, Any],
        parameterizable_candidates: List[Dict[str, Any]],
        correlations: List[Dict[str, Any]],
        dependencies: List[Dict[str, Any]],
        sub_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> CorrelationAdvice:
        """Classify captured values and produce correlation guidance.

        Args:
            target_url: Application URL analyzed by the pipeline.
            user_steps: Ordered browser journey steps.
            credentials: Credential values keyed by logical names.
            run1: First captured journey run.
            run2: Second captured journey run.
            parameterizable_candidates: Deterministically detected parameters.
            correlations: Dynamic values detected between runs.
            dependencies: Traced extract-to-pass relationships.
            sub_tasks: Optional orchestrated journey phases.

        Returns:
            Structured correlation advice, or deterministic cookie guidance if
            structured LLM classification fails.
        """
        prompt = _load_prompt()
        inputs = {
            "target_url": target_url,
            "credential_keys": json.dumps(list((credentials or {}).keys())),
            "journey_summary": _journey_summary(user_steps, sub_tasks),
            "fill_steps_json": json.dumps(_fill_steps_evidence(user_steps), indent=2),
            "params_json": json.dumps(
                [
                    {
                        "var": c.get("variable_name"),
                        "selector": c.get("selector"),
                        "value": _truncate(c.get("value"), 60),
                    }
                    for c in (parameterizable_candidates or [])[:30]
                ],
                indent=2,
            ),
            "deps_json": json.dumps(
                [
                    {
                        "var": d.get("value_key"),
                        "type": d.get("correlation_type"),
                        "from": _truncate(d.get("source_location"), 80),
                        "to": _truncate(d.get("target_location"), 80),
                    }
                    for d in (dependencies or [])[:30]
                ],
                indent=2,
            ),
            "dynamics_json": json.dumps(_dynamic_sample(correlations), indent=2),
            "cookies_json": json.dumps(
                {
                    "jar_diff": _cookie_pairs(run1, run2),
                    "set_cookie_names_run1": _set_cookie_names_from_requests(
                        run1.get("network_requests") or []
                    ),
                },
                indent=2,
            ),
            "response_snippets_json": json.dumps(
                _response_snippets(run1.get("network_requests") or []),
                indent=2,
            ),
        }

        try:
            # Request schema-constrained output so downstream promotion can use
            # validated models instead of parsing free-form LLM prose.
            advice = await self.router.ainvoke_with_failover(
                TaskType.EXTRACTION,
                lambda model: prompt
                | model.with_structured_output(CorrelationAdvice, method="json_schema"),
                inputs,
                config={
                    "run_name": "correlation_classifier",
                    "tags": ["correlation", "parameter", "cookie"],
                },
            )
            if isinstance(advice, CorrelationAdvice):
                return advice
            if isinstance(advice, dict):
                # Some providers return decoded JSON despite the schema wrapper.
                return CorrelationAdvice.model_validate(advice)
        except Exception as exc:
            logger.warning("LLM correlation classification failed: %s", exc)

        return self._fallback_cookie_notes(run1, run2)

    def _fallback_cookie_notes(
        self, run1: Dict[str, Any], run2: Dict[str, Any]
    ) -> CorrelationAdvice:
        """Derive conservative cookie advice without an LLM.

        Args:
            run1: First captured journey run.
            run2: Second captured journey run.

        Returns:
            Advice marking changed cookies for correlation verification.
        """
        notes = []
        for row in _cookie_pairs(run1, run2):
            if not row.get("changed"):
                continue
            notes.append(
                CookieRelationNote(
                    cookie_name=row["name"],
                    confidence="uncertain",
                    must_correlate=True,
                    extract_hint="Set-Cookie / browser cookie jar after login",
                    pass_to_hint="Cookie header on subsequent authenticated requests",
                    note=(
                        f"Cookie `{row['name']}` changed between runs. "
                        "Enable cookie jar persistence in the load script and verify "
                        "this cookie is required for authenticated APIs."
                    ),
                )
            )
        return CorrelationAdvice(
            fill_classifications=[],
            cookie_notes=notes[:15],
            needs_extra_run=False,
            summary="Deterministic cookie-diff fallback (LLM classifier unavailable).",
        )


def apply_correlation_advice(
    *,
    advice: CorrelationAdvice,
    user_steps: List[Any],
    parameterizable_candidates: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Promote LLM-marked UI correlations out of the parameter list.

    Args:
        advice: Validated classifier recommendations.
        user_steps: Ordered journey steps used to locate matching selectors.
        parameterizable_candidates: Existing parameter candidates.
        correlations: Existing dynamic-value records, updated in place.
        dependencies: Existing dependency records, updated in place.

    Returns:
        A tuple of retained parameters, correlations, and dependencies.
    """
    promote: Dict[str, FillClassification] = {}
    for fc in advice.fill_classifications or []:
        if fc.classification != "correlation":
            continue
        key = (fc.selector or "").strip()
        if key:
            promote[key] = fc

    if not promote:
        return parameterizable_candidates, correlations, dependencies

    kept_params = []
    for cand in parameterizable_candidates or []:
        sel = str(cand.get("selector") or "").strip()
        if sel in promote:
            continue
        kept_params.append(cand)

    # Deduplicate against deterministic traces before adding LLM-derived UI
    # dependencies, since both paths may identify the same selector.
    existing = {
        (
            d.get("value_key"),
            d.get("ui_selector") or d.get("target_location"),
        )
        for d in dependencies
    }

    for step_idx, step in enumerate(user_steps or []):
        if not isinstance(step, dict) or step.get("action") not in ("fill", "select"):
            continue
        sel = str(step.get("selector") or "").strip()
        if sel not in promote:
            continue
        fc = promote[sel]
        var = (
            fc.variable_name
            or suggest_correlation_var_name(sel, "correlated_value")
        )
        var = re.sub(r"[^a-zA-Z0-9_]", "_", var).strip("_") or "correlated_value"
        value = str(step.get("value") or "")
        if is_placeholder_value(value):
            value = ""
        dep_key = (var, sel)
        if dep_key in existing:
            continue
        existing.add(dep_key)
        dependencies.append(
            {
                "source_request": "",
                "source_location": "ui.page_text",
                "source_step_index": max(0, step_idx - 1),
                "source_step_action": "submit_or_create",
                "target_request": "",
                "target_location": f"fill.{sel}",
                "target_step_index": step_idx,
                "target_step_action": step.get("action", "fill"),
                "value_key": var,
                "run1_value": value,
                "run2_value": value,
                "correlation_type": "ui_extract",
                "confidence": "medium",
                "ui_selector": sel,
                "llm_reason": fc.reason,
            }
        )
        correlations.append(
            {
                "request_url": sel,
                "method": "FILL",
                "location": "ui_fill",
                "key": var,
                "dynamic_name": var,
                "run1_value": value,
                "run2_value": value,
                "reason": fc.reason or "LLM classified as correlation",
                "step_index": step_idx,
                "step_action": "fill",
            }
        )

    return kept_params, correlations, dependencies


def format_cookie_notes_section(notes: List[CookieRelationNote] | List[Dict]) -> str:
    """Render cookie guidance as a Markdown report section.

    Args:
        notes: Cookie note models or equivalent dictionaries.

    Returns:
        A Markdown table, or default cookie-jar advice when no notes exist.
    """
    if not notes:
        return (
            "_No explicit cookie correlation notes. "
            "Still enable the HTTP cookie jar in load scripts after login._\n"
        )
    lines = [
        "| Cookie | Must correlate? | Confidence | Extract / Pass | Note |",
        "| --- | --- | --- | --- | --- |",
    ]
    for n in notes:
        if isinstance(n, CookieRelationNote):
            data = n.model_dump()
        else:
            data = n
        extract_pass = (
            f"{data.get('extract_hint') or '—'} → {data.get('pass_to_hint') or '—'}"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{data.get('cookie_name', '')}`",
                    "yes" if data.get("must_correlate") else "verify",
                    str(data.get("confidence") or "uncertain"),
                    extract_pass.replace("|", "\\|"),
                    str(data.get("note") or "").replace("|", "\\|")[:200],
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append(
        "_If confidence is **uncertain**, keep cookie jar auto-handling enabled "
        "and confirm which cookie names your app requires for auth._"
    )
    return "\n".join(lines) + "\n"
