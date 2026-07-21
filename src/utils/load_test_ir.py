"""
Deterministic Load-Test Intermediate Representation (IR).

Pipeline:
  capture + params + correlations + TXNs  →  build_load_test_ir()  →  emit_k6(ir)

No LLM is involved. Same IR always produces the same k6 script.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


CSRF_TOKEN_REGEX = (
    r"""name=["']_token["'][^>]*value=["']([^"']+)|"""
    r"""value=["']([^"']+)["'][^>]*name=["']_token["']"""
)


def _safe_ident(name: str, fallback: str = "value") -> str:
    """Normalize text into an emitter-safe identifier.

    Args:
        name: Desired variable or transaction name.
        fallback: Replacement or prefix for invalid names.

    Returns:
        Identifier containing only letters, digits, and underscores.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name or "")
    cleaned = cleaned.strip("_") if cleaned.strip("_") else cleaned.replace("_", "x")
    # Preserve intentional leading underscore names by normalizing instead of strip
    if (name or "").startswith("_") and not cleaned.startswith("_"):
        cleaned = f"nfe_{cleaned}" if cleaned else "nfe_value"
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
    Digit-only IDs and short text params are replaced only as path segments or
    full query values so ``jo`` cannot corrupt ``joj`` → ``${nameorid}j``.
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
        # Path segment: /{literal}/ or /{literal}? or /{literal}$
        out = re.sub(
            rf"(?<=/)({re.escape(literal)})(?=/|\?|$)",
            placeholder,
            out,
        )
        # Query value: ={literal}& or ={literal}$ (full value only)
        out = re.sub(
            rf"(=)({re.escape(literal)})(?=&|$)",
            rf"\1{placeholder}",
            out,
        )
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


def _query_fingerprint(url: str) -> Tuple[str, str, Tuple[str, ...]]:
    """Fingerprint (scheme+host+path, method-agnostic) + sorted query keys."""
    try:
        p = urlparse(url or "")
        base = f"{p.scheme}://{p.netloc}{p.path}"
        keys = tuple(sorted({k for k, _ in parse_qsl(p.query, keep_blank_values=True)}))
        return base, p.query, keys
    except Exception:
        return url or "", "", ()


def _fix_placeholder_leakage(url: str) -> str:
    """Strip leftover chars after ``${var}`` from bad substring substitution.

    Example: ``nameOrId=${nameorid}h`` → ``nameOrId=${nameorid}``.
    """
    if not url or "${" not in url:
        return url
    return re.sub(
        r"(\$\{[A-Za-z_][A-Za-z0-9_]*\})[A-Za-z0-9._-]+(?=&|/|\?|$)",
        r"\1",
        url,
    )


def _coalesce_typeahead_requests(requests: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep the last request per (method, path, query-keys) autocomplete group.

    Typeahead fires ``nameOrId=j``, ``jo``, ``joh`` — only the final value belongs
    in a load script.
    """
    if len(requests) <= 1:
        return requests
    # Autocomplete-ish query params
    typeahead_keys = {"nameorid", "name", "q", "query", "search", "term", "keyword"}
    last_idx: Dict[Tuple[str, str, Tuple[str, ...]], int] = {}
    for i, r in enumerate(requests):
        method = str(r.get("method") or "GET").upper()
        if method != "GET":
            continue
        url = _fix_placeholder_leakage(str(r.get("url") or ""))
        r["url"] = url
        base, _q, keys = _query_fingerprint(url)
        if not keys or not any(k.lower() in typeahead_keys for k in keys):
            continue
        last_idx[(method, base, keys)] = i
    drop: Set[int] = set()
    for i, r in enumerate(requests):
        method = str(r.get("method") or "GET").upper()
        if method != "GET":
            continue
        url = str(r.get("url") or "")
        base, _q, keys = _query_fingerprint(url)
        if not keys or not any(k.lower() in typeahead_keys for k in keys):
            continue
        keep = last_idx.get((method, base, keys))
        if keep is not None and i != keep:
            drop.add(i)
    # Always scrub placeholder leakage on every request URL
    cleaned: List[Dict[str, Any]] = []
    for i, r in enumerate(requests):
        if i in drop:
            continue
        item = dict(r)
        item["url"] = _fix_placeholder_leakage(str(item.get("url") or ""))
        cleaned.append(item)
    return cleaned


def _has_auth_post(transactions: List[Dict[str, Any]]) -> bool:
    """True when any transaction already includes an auth/login POST."""
    for txn in transactions:
        for r in txn.get("requests") or []:
            method = str(r.get("method") or "").upper()
            url = str(r.get("url") or "").lower()
            if method == "POST" and any(
                h in url for h in ("/auth/validate", "/auth/login", "/login", "/signin", "/session")
            ):
                return True
            body = r.get("body")
            if method == "POST" and isinstance(body, dict):
                keys = {str(k).lower() for k in body}
                if "password" in keys and ("username" in keys or "user" in keys or "email" in keys):
                    return True
    return False


def _credential_vars(vars_list: List[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Return (username_var, password_var) names from IR vars."""
    user_var = pwd_var = None
    for v in vars_list or []:
        name = str(v.get("name") or "")
        src = str(v.get("source") or "").lower()
        is_cred = bool(v.get("is_credential"))
        if not user_var and (
            name.lower() in ("username", "user", "email", "login")
            or "username" in src
            or (is_cred and "pass" not in name.lower())
        ):
            user_var = name
        if not pwd_var and (
            name.lower() in ("password", "passwd", "pwd")
            or "password" in src
            or (is_cred and "pass" in name.lower())
        ):
            pwd_var = name
    return user_var, pwd_var


def _ensure_auth_csrf(
    transactions: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    *,
    origin: str,
) -> None:
    """No-op for browser-mode login; keep CSRF only when protocol auth/validate exists."""
    has_browser_login = any(
        (t.get("mode") == "browser" and "login" in str(t.get("name") or "").lower())
        or t.get("synthesized") == "browser_login"
        for t in (transactions or [])
    )
    if has_browser_login:
        return
    csrf_var = "csrf_token"
    login_url = ""
    validate_url = ""
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            url = str(r.get("url") or "")
            method = str(r.get("method") or "").upper()
            if method == "GET" and "/auth/login" in url.lower():
                login_url = url
            if method == "POST" and "/auth/validate" in url.lower():
                validate_url = url
                body = r.get("body")
                if isinstance(body, dict):
                    token_val = body.get("_token")
                    needs = token_val is None or (
                        isinstance(token_val, str)
                        and "csrf_token" not in token_val
                        and token_val in ("", "${_token}", "${token}")
                    )
                    if needs or token_val is None:
                        body = dict(body)
                        body["_token"] = f"${{{csrf_var}}}"
                        r["body"] = body
                    if not r.get("body_type") or r.get("body_type") == "empty":
                        r["body_type"] = "form"
    if not login_url and origin:
        login_url = f"{origin}/web/index.php/auth/login"
    if not validate_url:
        return
    for c in correlations:
        if str(c.get("var") or "") in ("_token", "token"):
            c["var"] = csrf_var
    if not any(str(c.get("var") or "") == csrf_var for c in correlations):
        correlations.append(
            {
                "var": csrf_var,
                "extract": {
                    "from_request": login_url,
                    "from_location": f"body.regex:{CSRF_TOKEN_REGEX}",
                    "from_step": -1,
                },
                "pass": {
                    "to_request": validate_url,
                    "to_location": "body._token",
                    "to_step": -1,
                },
                "correlation_type": "response_extract",
                "confidence": "high",
                "auto_cookie": False,
                "synthesized": True,
            }
        )


def _inject_missing_auth(
    transactions: List[Dict[str, Any]],
    *,
    origin: str,
    vars_list: List[Dict[str, Any]],
    target_url: str,
    correlations: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Ensure login establishes a real session before protocol API calls.

    Vue/SPA apps (e.g. OrangeHRM) inject CSRF ``_token`` via JavaScript, so a
    protocol-only ``GET /auth/login`` has no token and ``POST /auth/validate``
    silently fails → every API returns 401. Prefer **browser-mode login**
    (fill + submit) and sync cookies into the http jar for later protocol TXNs.
    """
    if not origin:
        return transactions
    # Drop previously synthesized protocol auth (SPA CSRF cannot work over raw HTTP)
    for txn in transactions:
        reqs = list(txn.get("requests") or [])
        cleaned = [
            r
            for r in reqs
            if not str(r.get("synthesized") or "").startswith("auth")
        ]
        if len(cleaned) != len(reqs):
            txn["requests"] = cleaned
    if _has_auth_post(transactions):
        # Real captured form POST exists — keep protocol path
        return transactions
    user_var, pwd_var = _credential_vars(vars_list)
    if not user_var or not pwd_var:
        return transactions

    login_path = "/web/index.php/auth/login"
    try:
        tp = urlparse(target_url or "")
        if "/auth/" in (tp.path or ""):
            login_path = tp.path
    except Exception:
        pass
    login_url = f"{origin}{login_path}"

    ui_steps = [
        {"action": "navigate", "url": login_url, "selector": "", "value": None},
        {
            "action": "fill",
            "selector": 'input[name="username"]',
            "value": f"${{{user_var}}}",
            "url": "",
        },
        {
            "action": "fill",
            "selector": 'input[name="password"]',
            "value": f"${{{pwd_var}}}",
            "url": "",
        },
        {
            "action": "click",
            "selector": 'button[type="submit"]',
            "value": None,
            "url": "",
        },
        {"action": "wait_for_load", "selector": "", "value": None, "url": ""},
    ]

    out = [dict(t) for t in transactions]
    login_idx = next(
        (
            i
            for i, t in enumerate(out)
            if "login" in str(t.get("name") or "").lower()
        ),
        None,
    )
    login_txn = {
        "name": "login",
        "description": "Browser login (SPA CSRF cannot be extracted via protocol HTTP)",
        "mode": "browser",
        "think_time_s": 1,
        "requests": [],
        "ui_steps": ui_steps,
        "step_indices": [],
        "sync_cookies_to_http": True,
        "synthesized": "browser_login",
    }
    if login_idx is None:
        insert_at = next(
            (
                i
                for i, t in enumerate(out)
                if str(t.get("name") or "").lower() not in ("launch", "")
            ),
            0,
        )
        out.insert(insert_at, login_txn)
    else:
        existing = dict(out[login_idx])
        # Drop fake protocol stand-ins (dashboard GET / empty auth)
        existing_reqs = [
            r
            for r in (existing.get("requests") or [])
            if not (
                str(r.get("method") or "").upper() == "GET"
                and (
                    "/dashboard/" in str(r.get("url") or "").lower()
                    or "/auth/login" in str(r.get("url") or "").lower()
                )
            )
            and not str(r.get("synthesized") or "").startswith("auth")
        ]
        login_txn["requests"] = existing_reqs
        # Keep any real UI steps from Watch-me, prefer our credential fills
        prior_ui = [
            s
            for s in (existing.get("ui_steps") or [])
            if s.get("action") not in ("fill", "click", "navigate", "wait_for_load")
        ]
        login_txn["ui_steps"] = ui_steps + prior_ui
        out[login_idx] = login_txn

    # Drop protocol CSRF correlations — browser handles login
    if correlations is not None:
        correlations[:] = [
            c
            for c in correlations
            if str(c.get("var") or "") not in ("csrf_token", "_token", "token")
            or not c.get("synthesized")
        ]
    return out


def _retarget_create_id_extracts(correlations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract create-resource IDs from the create POST, not a later GET.

    Using a GET that already embeds the ID causes empty ``${requestId}`` → 404.
    """
    out: List[Dict[str, Any]] = []
    for c in correlations or []:
        item = dict(c)
        var = str(item.get("var") or "")
        var_l = var.lower()
        ex = dict(item.get("extract") or {})
        from_req = str(ex.get("from_request") or "")
        lower = from_req.lower()
        is_id_var = var_l in (
            "requestid",
            "request_id",
            "id",
            "claimid",
            "claim_id",
        ) or var_l.endswith("id") and "reference" not in var_l
        # GET .../requests/13 → POST .../requests (create)
        if is_id_var and re.search(r"/requests/\d+(/|\?|$)", lower):
            fixed = re.sub(r"/requests/\d+(?=/|\?|$)", "/requests", from_req)
            if fixed != from_req:
                ex["from_request"] = fixed
                loc = str(ex.get("from_location") or "")
                if "reference" not in var_l:
                    if not loc.startswith("body.") or "reference" in loc.lower():
                        ex["from_location"] = "body.$.data.id"
                item["extract"] = ex
                item["retargeted"] = True
        out.append(item)
    return out


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

        requests_ir = _coalesce_typeahead_requests(requests_ir)

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

    origin = _origin(target_url or "")
    corr_list = _retarget_create_id_extracts(corr_list)
    txn_list = _inject_missing_auth(
        txn_list,
        origin=origin,
        vars_list=vars_list,
        target_url=target_url or "",
        correlations=corr_list,
    )
    _ensure_auth_csrf(txn_list, corr_list, origin=origin)

    return {
        "version": 1,
        "target_url": target_url or "",
        "origin": origin,
        "vars": vars_list,
        "correlations": corr_list,
        "transactions": txn_list,
    }
