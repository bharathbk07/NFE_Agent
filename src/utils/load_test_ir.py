"""
Deterministic Load-Test Intermediate Representation (IR).

Pipeline:
  capture + params + correlations + TXNs  →  build_load_test_ir()  →  emit_k6(ir)

No LLM is involved. Same IR always produces the same k6 script.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from src.utils.http_body import content_type_from_headers, parse_post_data


def _safe_ident(name: str, fallback: str = "value") -> str:
    """Normalize text into an emitter-safe identifier.

    Args:
        name: Desired variable or transaction name.
        fallback: Replacement or prefix for invalid names.

    Returns:
        Identifier containing only letters, digits, and underscores.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name or "").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    return cleaned


def _origin(url: str) -> str:
    """Extract the scheme and authority from a URL.

    Args:
        url: URL-like input.

    Returns:
        Origin string or an empty string when parsing is incomplete.
    """
    try:
        p = urlparse(url or "")
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def _lookup_request(
    network_requests: List[Dict[str, Any]],
    *,
    method: str,
    url: str,
    step_indices: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the best captured request for a transaction entry.

    Args:
        network_requests: Full captured request dictionaries.
        method: HTTP method to match case-insensitively.
        url: URL to match with trailing-slash tolerance.
        step_indices: Optional preferred journey indices.

    Returns:
        Preferred matching request, first URL/method match, or ``None``.
    """
    method_u = (method or "GET").upper()
    url_n = (url or "").rstrip("/")
    candidates = []
    for req in network_requests or []:
        if (req.get("method") or "GET").upper() != method_u:
            continue
        ru = (req.get("url") or "").rstrip("/")
        if ru != url_n and req.get("url") != url:
            continue
        if step_indices is not None:
            try:
                si = int(req.get("step_index", -999))
            except Exception:
                si = -999
            if si not in step_indices and not (si == -1 and -1 in step_indices):
                # still allow if URL uniquely matches
                pass
        candidates.append(req)
    if not candidates:
        return None
    if step_indices is not None:
        for req in candidates:
            try:
                if int(req.get("step_index", -999)) in step_indices:
                    return req
            except Exception:
                continue
    return candidates[0]


def _param_placeholders(
    body: Any,
    vars_by_value: Dict[str, str],
) -> Any:
    """Replace exact body leaf values with emitter placeholders.

    Args:
        body: Nested JSON-compatible request body.
        vars_by_value: Literal values mapped to normalized variable names.

    Returns:
        Body of the same nested shape with matching leaves replaced by
        ``${variable}`` strings.
    """
    if isinstance(body, dict):
        return {k: _param_placeholders(v, vars_by_value) for k, v in body.items()}
    if isinstance(body, list):
        return [_param_placeholders(v, vars_by_value) for v in body]
    if body is None:
        return None
    s = str(body)
    if s in vars_by_value:
        return f"${{{vars_by_value[s]}}}"
    return body


def _substitute_url_values(url: str, vars_by_value: Dict[str, str]) -> str:
    """Replace known correlation/parameter literals in a URL with ``${var}``.

    Longer values are applied first so partial overlaps prefer the full ID.
    Digit-only IDs are replaced only as path segments or query values so
    ``22`` cannot corrupt ``222``.
    """
    if not url or not vars_by_value:
        return url
    out = url
    for literal, var in sorted(
        vars_by_value.items(), key=lambda kv: len(kv[0] or ""), reverse=True
    ):
        if not literal:
            continue
        placeholder = f"${{{var}}}"
        if literal.isdigit():
            # Path segment: /{id}/ or /{id}? or /{id}$
            out = re.sub(
                rf"(?<=/)({re.escape(literal)})(?=/|\?|$)",
                placeholder,
                out,
            )
            # Query value: ={id}& or ={id}$
            out = re.sub(
                rf"(=)({re.escape(literal)})(?=&|$)",
                rf"\1{placeholder}",
                out,
            )
            continue
        if len(literal) < 2:
            continue
        if literal in out:
            out = out.replace(literal, placeholder)
    return out


def _substitute_headers(
    headers: Dict[str, str], vars_by_value: Dict[str, str]
) -> Dict[str, str]:
    """Replace known literals in header values with ``${var}`` placeholders."""
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        s = str(v)
        replaced = s
        for literal, var in sorted(
            vars_by_value.items(), key=lambda kv: len(kv[0] or ""), reverse=True
        ):
            if literal and literal in replaced:
                replaced = replaced.replace(literal, f"${{{var}}}")
        out[k] = replaced
    return out


def _headers_for_ir(headers: Dict[str, Any]) -> Dict[str, str]:
    """Filter captured headers to stable replay-safe IR headers.

    Args:
        headers: Captured request header mapping.

    Returns:
        String header mapping without transport/browser headers or cookies.
    """
    skip = {
        "host", "content-length", "connection", "accept-encoding",
        "user-agent", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
        "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user",
        "upgrade-insecure-requests",
    }
    out: Dict[str, str] = {}
    for k, v in (headers or {}).items():
        if str(k).lower() in skip:
            continue
        # Cookie jar is handled by k6 automatically — don't hardcode session cookies
        if str(k).lower() == "cookie":
            continue
        out[str(k)] = str(v)
    return out


def _infer_txn_mode(txn: Dict[str, Any], requests: List[Dict[str, Any]]) -> str:
    """Choose protocol or browser replay from transaction evidence.

    Prefer protocol when meaningful HTTP exists (API apps like OrangeHRM).
    Browser only when the phase is UI-only with little/no HTTP.
    """
    ui_steps = txn.get("ui_steps") or []
    if requests:
        return "protocol"
    if ui_steps:
        return "browser"
    return "protocol"


def build_load_test_ir(
    *,
    target_url: str,
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    network_requests: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build deterministic tool-agnostic Load-Test IR.

    Args:
        target_url: Journey target URL.
        parameterizable_candidates: Candidate user-fed values.
        dependencies: Extract-to-pass correlation edges.
        transactions: Analyzed transaction definitions.
        network_requests: Optional full captures used to recover bodies/headers.

    Returns:
        Versioned mapping with ``target_url``, ``origin``, ``vars``,
        ``correlations``, and normalized ``transactions``.
    """
    network_requests = network_requests or []

    # Excluding observed correlation values from *parameter* vars prevents
    # server output from becoming static CSV data. Correlation literals are
    # tracked separately so they can be substituted into URL/body/headers.
    vars_list: List[Dict[str, Any]] = []
    seen_vars: Set[str] = set()
    vars_by_value: Dict[str, str] = {}
    corr_values: Set[str] = set()
    corr_var_names: Set[str] = set()
    corr_by_value: Dict[str, str] = {}
    for dep in dependencies or []:
        var = _safe_ident(str(dep.get("value_key") or "token"), "token")
        if dep.get("value_key"):
            corr_var_names.add(var)
        for key in ("run1_value", "run2_value"):
            if dep.get(key):
                lit = str(dep[key])
                corr_values.add(lit)
                # Prefer longer / first-seen mapping for substitution
                if lit and lit not in corr_by_value:
                    # Skip person-name literals
                    if " " in lit and not lit.isdigit():
                        continue
                    # Allow short digit IDs (path segments); skip other 1-char noise
                    if len(lit) < 2 and not lit.isdigit():
                        continue
                    corr_by_value[lit] = var

    for cand in parameterizable_candidates or []:
        name = _safe_ident(cand.get("variable_name") or "input")
        value = "" if cand.get("value") is None else str(cand.get("value"))
        if value in corr_values or name in corr_var_names:
            continue
        if name in seen_vars:
            continue
        seen_vars.add(name)
        vars_list.append(
            {
                "name": name,
                "value": value,
                "source": cand.get("selector") or "",
                "is_credential": bool(cand.get("is_credential")),
                "propagations": cand.get("propagations") or [],
            }
        )
        if value and value not in vars_by_value:
            vars_by_value[value] = name

    # Combined map: params + correlations for URL/body/header substitution
    subst_by_value: Dict[str, str] = dict(vars_by_value)
    for lit, var in corr_by_value.items():
        if lit not in subst_by_value:
            subst_by_value[lit] = var

    # Stable first-seen deduplication keeps downstream emitter output reproducible.
    corr_list: List[Dict[str, Any]] = []
    seen_corr: Set[Tuple[Any, ...]] = set()
    for dep in dependencies or []:
        key = (
            dep.get("value_key"),
            dep.get("source_request"),
            dep.get("target_request"),
            dep.get("source_location"),
            dep.get("target_location"),
        )
        if key in seen_corr:
            continue
        seen_corr.add(key)
        ctype = dep.get("correlation_type") or "response_extract"
        if ctype == "ui_extract":
            confidence = "medium"
        elif ctype == "response_extract":
            confidence = "high"
        else:
            confidence = dep.get("confidence") or "low"
        # Cookie-jar style: k6 handles automatically
        src_loc = str(dep.get("source_location") or "")
        tgt_loc = str(dep.get("target_location") or "")
        auto_cookie = "set-cookie" in src_loc.lower() or tgt_loc.startswith("cookie.")
        corr_list.append(
            {
                "var": _safe_ident(dep.get("value_key") or "token", "token"),
                "extract": {
                    "from_request": dep.get("source_request"),
                    "from_location": src_loc,
                    "from_step": dep.get("source_step_index"),
                },
                "pass": {
                    "to_request": dep.get("target_request"),
                    "to_location": tgt_loc,
                    "to_step": dep.get("target_step_index"),
                },
                "correlation_type": ctype,
                "confidence": confidence,
                "auto_cookie": auto_cookie,
                "run1_value": dep.get("run1_value"),
                "run2_value": dep.get("run2_value"),
                "ui_selector": dep.get("ui_selector"),
            }
        )

    # Transaction order follows analysis order, as execution order is meaningful.
    txn_list: List[Dict[str, Any]] = []
    used_txn_names: Set[str] = set()
    for txn in transactions or []:
        base_name = _safe_ident(txn.get("name") or "Txn", "Txn")
        name = base_name
        suffix = 2
        while name in used_txn_names:
            name = f"{base_name}_{suffix}"
            suffix += 1
        used_txn_names.add(name)
        step_indices = txn.get("step_indices")
        entries = txn.get("http_entries") or []
        requests_ir: List[Dict[str, Any]] = []
        seen_req: Set[Tuple[str, str]] = set()

        for e in entries:
            if not isinstance(e, dict):
                continue
            method = (e.get("method") or "GET").upper()
            url = e.get("url") or ""
            if not url.startswith("http"):
                continue
            key = (method, url)
            if key in seen_req:
                continue
            seen_req.add(key)

            full = _lookup_request(
                network_requests,
                method=method,
                url=url,
                step_indices=step_indices,
            )
            body = None
            body_type = "empty"
            headers: Dict[str, str] = {}
            if full:
                raw_body = full.get("post_data")
                body_type = full.get("body_type") or ""
                if not body_type or body_type == "unknown":
                    parsed, body_type = parse_post_data(
                        raw_body, content_type_from_headers(full.get("headers") or {})
                    )
                    body = parsed
                else:
                    body = raw_body
                headers = _headers_for_ir(full.get("headers") or {})
            # Params + correlations into body/URL/headers (extract→pass)
            body = _param_placeholders(body, subst_by_value)
            url = _substitute_url_values(url, subst_by_value)
            headers = _substitute_headers(headers, subst_by_value)

            requests_ir.append(
                {
                    "method": method,
                    "url": url,
                    "headers": headers,
                    "body": body,
                    "body_type": body_type or "empty",
                    "resource_type": (full or e).get("resource_type") or "",
                    "status": (full or {}).get("status"),
                    "step_index": (full or {}).get("step_index", e.get("step_index")),
                }
            )

        ui_steps = [
            {
                "action": s.get("action"),
                "selector": s.get("selector") or "",
                "value": s.get("value"),
                "url": s.get("url") or "",
            }
            for s in (txn.get("ui_steps") or [])
            if isinstance(s, dict)
        ]
        corr_by_selector: Dict[str, str] = {}
        for dep in dependencies or []:
            sel = dep.get("ui_selector")
            var = dep.get("value_key")
            if sel and var:
                corr_by_selector[str(sel)] = _safe_ident(str(var), "token")
        # Substitute fill values: correlations first, then user parameters
        for s in ui_steps:
            if s.get("action") == "fill" and s.get("value") is not None:
                sv = str(s["value"])
                sel = str(s.get("selector") or "")
                if sel in corr_by_selector:
                    s["value"] = f"${{{corr_by_selector[sel]}}}"
                elif sv in vars_by_value:
                    s["value"] = f"${{{vars_by_value[sv]}}}"

        mode = _infer_txn_mode(txn, requests_ir)
        txn_list.append(
            {
                "name": name,
                "description": txn.get("description") or name,
                "mode": mode,
                "think_time_s": 1,
                "requests": requests_ir,
                "ui_steps": ui_steps,
                "step_indices": step_indices or [],
            }
        )

    return {
        "version": 1,
        "target_url": target_url or "",
        "origin": _origin(target_url or ""),
        "vars": vars_list,
        "correlations": corr_list,
        "transactions": txn_list,
    }
