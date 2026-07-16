"""
Parameter vs correlation classification for performance testing.

Parameters  — static test data fed into the script (username, password, remarks).
              Sourced from user input / data files; same across iterations unless
              you parameterize a CSV column.

Correlations — dynamic values the *server* generates per run (session tokens,
               claim reference IDs, CSRF tokens). Must be extracted from a prior
               response (or UI) and passed into later requests.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs

from src.utils.http_body import (
    content_type_from_headers,
    flatten_body_fields,
    parse_post_data,
)

# Field labels / selectors that indicate server-generated lookup values
CORRELATION_FIELD_RE = re.compile(
    r"(?:"
    r"reference[\s_-]*(?:id|no|num|number)?|"
    r"ref[\s_-]*(?:id|no|num|number)|"
    r"claim[\s_-]*(?:id|no|num|number|reference)|"
    r"order[\s_-]*(?:id|no|num|number)|"
    r"transaction[\s_-]*(?:id|no)|"
    r"confirmation[\s_-]*(?:no|number|code)|"
    r"booking[\s_-]*(?:id|ref|reference)|"
    r"invoice[\s_-]*(?:id|no|number)|"
    r"tracking[\s_-]*(?:id|no|number)"
    r")",
    re.IGNORECASE,
)

PLACEHOLDER_VALUE_RE = re.compile(
    r"^\s*(\{\{.*\}\}|\$\{.*\}|CORRELATE:.*)\s*$",
    re.IGNORECASE | re.DOTALL,
)

GENERATED_NUMERIC_ID_RE = re.compile(r"^\d{10,}$")
UUID_PREFIX_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-", re.IGNORECASE
)

CREATE_URL_HINTS = ("claim", "order", "submit", "create", "checkout", "invoice")


def is_placeholder_value(value: str) -> bool:
    """Test whether text is an explicit correlation placeholder.

    Args:
        value: Candidate field value.

    Returns:
        ``True`` for ``{{...}}``, ``${...}``, or ``CORRELATE:...`` forms.
    """
    return bool(PLACEHOLDER_VALUE_RE.match(str(value or "").strip()))


def is_correlation_field_selector(selector: str) -> bool:
    """Test whether a selector labels a likely server-generated field.

    Args:
        selector: Browser selector or embedded label text.

    Returns:
        Boolean match against known reference/identifier field patterns.
    """
    text = str(selector or "")
    if CORRELATION_FIELD_RE.search(text):
        return True
    label_match = re.search(
        r'has-text\(\s*["\']([^"\']+)["\']', text, re.IGNORECASE
    )
    if label_match and CORRELATION_FIELD_RE.search(label_match.group(1)):
        return True
    return False


def is_generated_id_value(value: str) -> bool:
    """Test whether a value resembles a generated numeric ID or UUID.

    Args:
        value: Candidate field value.

    Returns:
        ``True`` for long numeric IDs or UUID prefixes, excluding placeholders.
    """
    v = str(value or "").strip()
    if not v or is_placeholder_value(v):
        return False
    return bool(GENERATED_NUMERIC_ID_RE.match(v) or UUID_PREFIX_RE.match(v))


def suggest_correlation_var_name(selector: str, fallback: str = "dynamic_value") -> str:
    """Derive a safe correlation variable name from selector label text.

    Args:
        selector: Browser selector or human-readable label.
        fallback: Name returned when no correlation label is recognized.

    Returns:
        Lowercase underscore-delimited name, at most 40 characters for raw
        selector-derived names.
    """
    for pattern in (
        r'has-text\(\s*["\']([^"\']+)["\']',
        r'has-text\("([^"]+)"\)',
        r'label:has-text\("([^"]+)"\)',
    ):
        match = re.search(pattern, selector or "", re.IGNORECASE)
        if match:
            label = match.group(1).strip()
            if CORRELATION_FIELD_RE.search(label):
                cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", label.lower()).strip("_")
                if cleaned:
                    return cleaned
    if CORRELATION_FIELD_RE.search(selector or ""):
        cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", selector.lower()).strip("_")
        if cleaned:
            return cleaned[:40]
    return fallback


def _parse_json_paths(data: Any, current_path: str = "$") -> Dict[str, str]:
    """Flatten JSON leaves into JSONPath-like locations.

    Args:
        data: Nested JSON-compatible data.
        current_path: Path prefix used during recursion.

    Returns:
        Mapping from leaf paths to string values.
    """
    paths: Dict[str, str] = {}
    if isinstance(data, dict):
        for key, val in data.items():
            paths.update(_parse_json_paths(val, f"{current_path}.{key}"))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            paths.update(_parse_json_paths(item, f"{current_path}[{idx}]"))
    elif data is not None:
        paths[current_path] = str(data)
    return paths


def _value_in_text(needle: str, haystack: str) -> bool:
    """Check for a value in text while bounding short-token matches.

    Args:
        needle: Candidate value.
        haystack: Text to search.

    Returns:
        Boolean substring or whole-word match.
    """
    if not needle or not haystack:
        return False
    n = str(needle).strip()
    h = str(haystack)
    if len(n) <= 2:
        return bool(re.search(rf"\b{re.escape(n)}\b", h))
    return n in h


def find_value_in_response(
    value: str, response_body: str, response_headers: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """Locate a value in response headers, parsed JSON, or raw text.

    Args:
        value: Value to trace.
        response_body: Raw response body text.
        response_headers: Optional response headers.

    Returns:
        ``header.<name>``, ``body.<json-path>``, ``body.raw``, or ``None``.
    """
    if not value:
        return None
    for h_key, h_val in (response_headers or {}).items():
        if _value_in_text(value, str(h_val)):
            return f"header.{h_key}"
    body = response_body or ""
    if not body:
        return None
    try:
        parsed = json.loads(body)
        for path, found in _parse_json_paths(parsed).items():
            if found == str(value):
                return f"body.{path}"
    except Exception:
        pass
    if _value_in_text(value, body):
        return "body.raw"
    return None


def find_value_in_request(
    value: str, req: Dict[str, Any]
) -> Optional[str]:
    """Locate a value in an outbound request.

    Args:
        value: Value to trace.
        req: Captured request containing URL, body, and optional headers.

    Returns:
        Query/body location string or ``None``.
    """
    val = str(value).strip()
    if not val:
        return None
    try:
        parsed = urlparse(req.get("url", ""))
        for q_key, q_vals in parse_qs(parsed.query).items():
            if any(_value_in_text(val, qv) for qv in q_vals):
                return f"query.{q_key}"
    except Exception:
        pass

    post_data = req.get("post_data")
    if post_data is not None and post_data != "":
        if not isinstance(post_data, (dict, list)):
            post_data, _ = parse_post_data(
                post_data, content_type_from_headers(req.get("headers") or {})
            )
        for field_path, field_val in flatten_body_fields(post_data).items():
            if _value_in_text(val, field_val):
                return f"body.{field_path}"
        try:
            raw = (
                json.dumps(post_data)
                if isinstance(post_data, (dict, list))
                else str(post_data)
            )
            if _value_in_text(val, raw):
                return "body.raw"
        except Exception:
            pass
    return None


def _prior_submit_step_index(user_steps: List[Any], fill_index: int) -> int:
    """Find the nearest likely create/submit step before a fill.

    Args:
        user_steps: Ordered journey steps.
        fill_index: Index of the candidate correlation fill.

    Returns:
        Prior submit-like index, falling back to the immediately prior step.
    """
    for idx in range(fill_index - 1, -1, -1):
        step = user_steps[idx] if idx < len(user_steps) else {}
        if not isinstance(step, dict):
            continue
        action = (step.get("action") or "").lower()
        selector = (step.get("selector") or "").lower()
        sub_task = (step.get("sub_task") or "").lower()
        if "submit" in sub_task or "create" in sub_task:
            return idx
        if action == "click" and "submit" in selector:
            return idx
        if action == "wait_for_selector" and (
            "reference" in selector or "claim reference" in selector
        ):
            return idx
    return max(0, fill_index - 1)


def _find_create_source_request(
    requests: List[Dict[str, Any]], before_step: int
) -> Optional[Dict[str, Any]]:
    """Find the latest create-like mutation request before a journey step.

    Args:
        requests: Captured request dictionaries.
        before_step: Exclusive upper bound for request step indices.

    Returns:
        Latest matching POST/PUT/PATCH request, or ``None``.
    """
    candidates = []
    for req in requests or []:
        try:
            step_idx = int(req.get("step_index", -999))
        except Exception:
            step_idx = -999
        if step_idx >= before_step:
            continue
        method = (req.get("method") or "GET").upper()
        if method not in ("POST", "PUT", "PATCH"):
            continue
        url_lower = (req.get("url") or "").lower()
        if any(hint in url_lower for hint in CREATE_URL_HINTS):
            candidates.append((step_idx, req))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[-1][1]


def should_treat_fill_as_correlation(
    step: Dict[str, Any],
    step_index: int,
    user_steps: List[Any],
    run1_requests: List[Dict[str, Any]],
    credentials: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Classify a fill as server-sourced and derive its variable name.

    Args:
        step: Fill step with selector and value.
        step_index: Step position in the journey.
        user_steps: Full journey step list.
        run1_requests: First-run network captures.
        credentials: Optional credential values to exclude.

    Returns:
        Suggested correlation variable name, or ``None`` for a parameter.
    """
    value = str(step.get("value") or "").strip()
    selector = str(step.get("selector") or "")
    if not value or not selector:
        return None
    if is_placeholder_value(value):
        return suggest_correlation_var_name(selector, "correlated_value")

    creds = credentials or {}
    val_lower = value.lower()
    for cred_val in creds.values():
        if cred_val and str(cred_val).lower() in val_lower:
            return None

    # Explicit correlation field (Reference Id, order id, etc.)
    if is_correlation_field_selector(selector):
        return suggest_correlation_var_name(selector, "reference_id")

    # Value appears in a prior HTTP response → server generated
    for req in run1_requests or []:
        try:
            req_step = int(req.get("step_index", 999))
        except Exception:
            req_step = 999
        if req_step >= step_index:
            continue
        origin = find_value_in_response(
            value,
            req.get("response_body") or "",
            req.get("response_headers") or {},
        )
        if origin:
            return suggest_correlation_var_name(selector, "extracted_value")

    # Long numeric / UUID after create-submit flow
    if is_generated_id_value(value) and step_index > 0:
        prior_sub = _prior_submit_step_index(user_steps, step_index)
        if prior_sub < step_index:
            return suggest_correlation_var_name(selector, "generated_id")

    return None


def trace_fill_correlations(
    user_steps: List[Any],
    run1_requests: List[Dict[str, Any]],
    run2_requests: Optional[List[Dict[str, Any]]] = None,
    credentials: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Trace server-generated fills into correlation records and dependencies.

    Args:
        user_steps: Ordered journey steps.
        run1_requests: First-run network captures.
        run2_requests: Optional second-run captures.
        credentials: Optional values that must remain parameters.

    Returns:
        Pair of correlation dictionaries and extract-to-pass dependencies.
    """
    dependencies: List[Dict[str, Any]] = []
    correlations: List[Dict[str, Any]] = []
    run2_requests = run2_requests or []
    seen: Set[Tuple[str, str]] = set()

    for step_index, step in enumerate(user_steps or []):
        if not isinstance(step, dict) or step.get("action") != "fill":
            continue

        value = str(step.get("value") or "").strip()
        selector = str(step.get("selector") or "")
        var_name = should_treat_fill_as_correlation(
            step, step_index, user_steps, run1_requests, credentials
        )
        if not var_name:
            continue

        # Resolve literal value for tracing (skip pure placeholders)
        trace_value = value
        if is_placeholder_value(value):
            trace_value = ""

        source_req: Optional[Dict[str, Any]] = None
        source_location = "ui.page_text"
        run1_value = trace_value
        run2_value = trace_value

        if trace_value:
            for req in run1_requests or []:
                try:
                    req_step = int(req.get("step_index", 999))
                except Exception:
                    req_step = 999
                if req_step >= step_index:
                    continue
                loc = find_value_in_response(
                    trace_value,
                    req.get("response_body") or "",
                    req.get("response_headers") or {},
                )
                if loc:
                    source_req = req
                    source_location = loc
                    break

        # Fall back to the nearest create request, then UI text, because recorder
        # steps can carry a generated value even when response bodies were absent.
        if source_req is None:
            source_req = _find_create_source_request(run1_requests, step_index)
            if source_req and trace_value:
                loc = find_value_in_response(
                    trace_value,
                    source_req.get("response_body") or "",
                    source_req.get("response_headers") or {},
                )
                if loc:
                    source_location = loc
                else:
                    source_location = "ui.page_text"
            else:
                source_location = "ui.page_text"

        target_req_url = ""
        target_location = f"fill.{selector}"
        target_step = step_index

        if trace_value:
            for req in run1_requests or []:
                try:
                    req_step = int(req.get("step_index", -1))
                except Exception:
                    req_step = -1
                if req_step < step_index:
                    continue
                loc = find_value_in_request(trace_value, req)
                if loc:
                    target_req_url = req.get("url") or ""
                    target_location = loc
                    target_step = req_step
                    break

        source_step = (
            int(source_req.get("step_index", -1))
            if source_req
            else _prior_submit_step_index(user_steps, step_index)
        )
        source_action = (
            source_req.get("step_action", "unknown")
            if source_req
            else (user_steps[source_step].get("action", "unknown")
                  if source_step < len(user_steps) else "unknown")
        )

        dep_key = (var_name, source_location, target_location)
        if dep_key in seen:
            continue
        seen.add(dep_key)

        ctype = (
            "response_extract"
            if source_location.startswith(("body.", "header."))
            and source_req
            else "ui_extract"
        )
        dep = {
            "source_request": (source_req or {}).get("url") or "",
            "source_location": source_location,
            "source_step_index": source_step,
            "source_step_action": source_action,
            "target_request": target_req_url,
            "target_location": target_location,
            "target_step_index": target_step,
            "target_step_action": step.get("action", "fill"),
            "value_key": var_name,
            "run1_value": run1_value,
            "run2_value": run2_value,
            "correlation_type": ctype,
            "confidence": "high" if ctype == "response_extract" else "medium",
            "ui_selector": selector,
        }
        dependencies.append(dep)
        correlations.append({
            "request_url": target_req_url or selector,
            "method": "FILL",
            "location": "ui_fill",
            "key": var_name,
            "dynamic_name": var_name,
            "run1_value": run1_value,
            "run2_value": run2_value,
            "reason": "Server-generated value used in a later search/form field",
            "step_index": step_index,
            "step_action": "fill",
        })

    return correlations, dependencies


def filter_parameters(
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    user_steps: List[Any],
    run1_requests: List[Dict[str, Any]],
    credentials: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Remove candidates reconciled as server-generated correlations.

    Args:
        parameterizable_candidates: Candidate parameter dictionaries.
        dependencies: Reconciled extract-to-pass edges.
        user_steps: Ordered journey steps.
        run1_requests: First-run network captures.
        credentials: Optional known credentials.

    Returns:
        Parameter dictionaries that remain user-fed test data.
    """
    corr_values: Set[str] = set()
    corr_vars: Set[str] = set()
    corr_selectors: Set[str] = set()
    for dep in dependencies or []:
        if dep.get("run1_value"):
            corr_values.add(str(dep["run1_value"]))
        if dep.get("run2_value"):
            corr_values.add(str(dep["run2_value"]))
        if dep.get("value_key"):
            corr_vars.add(str(dep["value_key"]))
        if dep.get("ui_selector"):
            corr_selectors.add(str(dep["ui_selector"]))

    filtered: List[Dict[str, Any]] = []
    for step_idx, cand in enumerate(parameterizable_candidates or []):
        value = str(cand.get("value") or "").strip()
        selector = str(cand.get("selector") or "")

        if is_placeholder_value(value):
            continue
        if value in corr_values or cand.get("variable_name") in corr_vars:
            continue
        if selector in corr_selectors:
            continue
        if should_treat_fill_as_correlation(
            {"action": "fill", "value": value, "selector": selector},
            step_idx,
            user_steps,
            run1_requests,
            credentials,
        ):
            continue
        filtered.append(cand)
    return filtered


def reconcile_analysis(
    user_steps: List[Any],
    parameterizable_candidates: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    run1_requests: List[Dict[str, Any]],
    run2_requests: Optional[List[Dict[str, Any]]] = None,
    credentials: Optional[Dict[str, str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Reconcile fill tracing with diff-based analysis.

    Args:
        user_steps: Ordered journey steps.
        parameterizable_candidates: Initial parameter candidates.
        correlations: Existing dynamic-value records, mutated with unique fills.
        dependencies: Existing edges, mutated with unique fill dependencies.
        run1_requests: First-run captures.
        run2_requests: Optional second-run captures.
        credentials: Optional known credentials.

    Returns:
        Tuple of filtered parameters, merged correlations, and merged
        dependencies.
    """
    fill_corrs, fill_deps = trace_fill_correlations(
        user_steps, run1_requests, run2_requests, credentials
    )

    # Reconcile by semantic edge identity, preserving first-seen ordering from
    # the primary two-run analysis.
    dep_keys = {
        (
            d.get("value_key"),
            d.get("source_location"),
            d.get("target_location"),
        )
        for d in dependencies
    }
    for dep in fill_deps:
        key = (dep.get("value_key"), dep.get("source_location"), dep.get("target_location"))
        if key not in dep_keys:
            dependencies.append(dep)
            dep_keys.add(key)

    corr_keys = {(c.get("dynamic_name"), c.get("key")) for c in correlations}
    for corr in fill_corrs:
        key = (corr.get("dynamic_name"), corr.get("key"))
        if key not in corr_keys:
            correlations.append(corr)
            corr_keys.add(key)

    params = filter_parameters(
        parameterizable_candidates,
        dependencies,
        user_steps,
        run1_requests,
        credentials,
    )
    return params, correlations, dependencies
