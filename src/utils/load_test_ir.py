"""
Deterministic Load-Test Intermediate Representation (IR).

Pipeline:
  capture + params + correlations + TXNs  →  build_load_test_ir()  →  emit_k6(ir)

No LLM is involved. Same IR always produces the same k6 script.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.utils.http_body import content_type_from_headers, parse_post_data


CSRF_TOKEN_REGEX = (
    r"""name=["']_token["'][^>]*value=["']([^"']+)|"""
    r"""value=["']([^"']+)["'][^>]*name=["']_token["']"""
)

DEFAULT_THINK_TIME_S: Dict[str, float] = {"min": 1.0, "max": 3.0}

_STATIC_RESOURCE_TYPES = frozenset(
    {"stylesheet", "script", "image", "font", "media", "manifest", "other"}
)
_XHR_LIKE_TYPES = frozenset({"xhr", "fetch", "document", "xmlhttprequest", ""})
_STABLE_JSON_KEYS = ("success", "ok", "status", "data", "result", "message")
_DYNAMIC_TOKEN_RE = re.compile(
    r"(?i)^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|\d{6,}"
    r"|[A-Za-z0-9_-]{32,})$"
)
_TITLE_RE = re.compile(r"<title[^>]*>\s*([^<]{3,80})\s*</title>", re.I)
_HEAVY_DIGIT_RE = re.compile(r"\d{5,}|[0-9a-f]{8}-[0-9a-f]{4}", re.I)


def normalize_think_time_s(value: Any) -> Dict[str, float]:
    """Normalize IR think time to ``{min, max}`` seconds.

    Accepts a number (fixed delay), a ``{min, max}`` mapping, or ``None``
    (defaults to :data:`DEFAULT_THINK_TIME_S`).
    """
    if value is None:
        return dict(DEFAULT_THINK_TIME_S)
    if isinstance(value, dict):
        try:
            lo = float(value.get("min", value.get("max", 1)))
            hi = float(value.get("max", value.get("min", lo)))
        except (TypeError, ValueError):
            return dict(DEFAULT_THINK_TIME_S)
        if lo < 0:
            lo = 0.0
        if hi < lo:
            hi = lo
        return {"min": lo, "max": hi}
    try:
        n = float(value)
    except (TypeError, ValueError):
        return dict(DEFAULT_THINK_TIME_S)
    if n < 0:
        n = 0.0
    return {"min": n, "max": n}


def _url_path_sig(url: str) -> str:
    """Path signature with ``${var}`` and numeric ID segments wildcarded."""
    try:
        path = urlparse(url or "").path
    except Exception:
        path = url or ""
    segs = []
    for s in path.split("/"):
        if not s:
            continue
        if s.isdigit() or re.fullmatch(r"\$\{[^}]+\}", s):
            segs.append("{id}")
        else:
            segs.append(s)
    return "/" + "/".join(segs)


def _urls_match_loose(a: str, b: str) -> bool:
    """Match URLs ignoring trailing slashes, query, placeholders, and path IDs."""
    if not a or not b:
        return False
    if a.rstrip("/") == b.rstrip("/"):
        return True
    try:
        pa = urlparse(re.sub(r"\$\{[^}]+\}", "X", a))
        pb = urlparse(re.sub(r"\$\{[^}]+\}", "X", b))
        if pa.netloc == pb.netloc and pa.path.rstrip("/") == pb.path.rstrip("/"):
            return True
        if pa.netloc == pb.netloc and _url_path_sig(a) == _url_path_sig(b):
            return True
    except Exception:
        return False
    return False


def select_anchor_request_index(
    requests: List[Dict[str, Any]],
    correlations: Optional[List[Dict[str, Any]]] = None,
) -> Optional[int]:
    """Pick one important request index for a TXN content assertion.

    Priority: correlation extract source → mutating method → last XHR/document
    GET → last non-soft request.
    """
    if not requests:
        return None
    corrs = correlations or []

    def _is_mocked(r: Dict[str, Any]) -> bool:
        return bool(
            r.get("mock_in_load_test")
            or r.get("requires_manual_data")
            or r.get("ir_flag") == "manual_test_data_or_mock"
        )

    # 1) Correlation extract sources
    for i, r in enumerate(requests):
        if _is_mocked(r) or r.get("soft_check"):
            continue
        url = str(r.get("url") or "")
        for c in corrs:
            if c.get("auto_cookie"):
                continue
            from_req = str((c.get("extract") or {}).get("from_request") or "")
            if from_req and _urls_match_loose(from_req, url):
                return i

    # 2) First mutating method
    for i, r in enumerate(requests):
        method = str(r.get("method") or "GET").upper()
        if method in ("POST", "PUT", "PATCH", "DELETE") and not _is_mocked(r):
            if not r.get("soft_check"):
                return i

    # 3) Last non-static XHR/document GET
    last_xhr: Optional[int] = None
    for i, r in enumerate(requests):
        method = str(r.get("method") or "GET").upper()
        if method != "GET" or _is_mocked(r) or r.get("soft_check"):
            continue
        rt = str(r.get("resource_type") or "").lower()
        if rt in _STATIC_RESOURCE_TYPES:
            continue
        if rt in _XHR_LIKE_TYPES or not rt:
            last_xhr = i
    if last_xhr is not None:
        return last_xhr

    # 4) Fallback: last non-soft request
    for i in range(len(requests) - 1, -1, -1):
        r = requests[i]
        if not r.get("soft_check") and not _is_mocked(r):
            return i
    return len(requests) - 1


def _json_path_exists_on(data: Any, path: str) -> bool:
    """Return True when ``path`` (``$.a.b`` / ``a.0.b``) resolves on ``data``."""
    p = str(path or "").strip()
    if p.startswith("$."):
        p = p[2:]
    elif p.startswith("$"):
        p = p[1:].lstrip(".")
    if not p:
        return data is not None
    cur: Any = data
    for part in p.split("."):
        if cur is None:
            return False
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False
            continue
        if isinstance(cur, dict):
            if part not in cur:
                return False
            cur = cur[part]
            continue
        return False
    return cur is not None


def derive_request_assertion(
    request: Dict[str, Any],
    *,
    captured: Optional[Dict[str, Any]] = None,
    correlations: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a stable content assertion from a captured response (no dynamic values)."""
    full = captured or {}
    status_raw = full.get("status", request.get("status"))
    expect_status: List[int] = []
    try:
        status_i = int(status_raw) if status_raw is not None else 0
    except (TypeError, ValueError):
        status_i = 0
    if 200 <= status_i < 400:
        expect_status = [status_i]
        if status_i == 200:
            expect_status = [200, 201]
        elif status_i in (301, 302, 303, 307, 308):
            expect_status = [status_i, 200]
    else:
        expect_status = [200, 201, 204]

    assertion: Dict[str, Any] = {
        "type": "status_and_body",
        "expect_status": expect_status,
    }

    body = full.get("response_body")
    if body is None:
        body = request.get("response_body") or ""
    if not isinstance(body, str):
        try:
            body = json.dumps(body)
        except Exception:
            body = str(body)

    method = str(request.get("method") or "GET").upper()
    url = str(request.get("url") or "")
    if method == "POST" and "/auth/validate" in url.lower():
        assertion["require_auth_session"] = True
        return assertion

    data = None
    if body.strip().startswith("{") or body.strip().startswith("["):
        try:
            data = json.loads(body)
        except Exception:
            data = None

    json_paths: List[str] = []
    url_ir = str(request.get("url") or "")
    for c in correlations or []:
        if c.get("auto_cookie"):
            continue
        ex = c.get("extract") or {}
        from_req = str(ex.get("from_request") or "")
        loc = str(ex.get("from_location") or "")
        if not from_req or not _urls_match_loose(from_req, url_ir):
            continue
        if loc.startswith("body.$") or (
            loc.startswith("body.") and not loc.startswith("body.regex:")
        ):
            path = loc[len("body.") :] if loc.startswith("body.") else loc
            if path.startswith("$"):
                jp = path
            else:
                jp = f"$.{path}"
            if data is None or _json_path_exists_on(data, jp):
                if jp not in json_paths:
                    json_paths.append(jp)

    if isinstance(data, dict):
        for key in _STABLE_JSON_KEYS:
            if key not in data:
                continue
            jp = f"$.{key}"
            if jp not in json_paths:
                json_paths.append(jp)
        if json_paths:
            assertion["json_path_exists"] = json_paths[:4]
            return assertion

    if isinstance(data, list) and data:
        assertion["json_path_exists"] = ["$.0"]
        return assertion

    if body and ("<html" in body.lower() or "<!doctype" in body.lower()):
        title_m = _TITLE_RE.search(body)
        contains: List[str] = []
        if title_m:
            title = title_m.group(1).strip()
            if title and not _HEAVY_DIGIT_RE.search(title):
                contains.append(f"<title>{title}</title>")
        if not contains:
            # Prefer a short static phrase without UUID/long digits
            for chunk in re.findall(r">([^<]{12,60})<", body):
                text = " ".join(chunk.split())
                if len(text) < 12 or _HEAVY_DIGIT_RE.search(text):
                    continue
                if _DYNAMIC_TOKEN_RE.match(text.strip()):
                    continue
                contains.append(text[:60])
                break
        if contains:
            assertion["body_contains"] = contains[:2]
        # Success pages should not still show the login form
        if re.search(r'name=["\']username["\']', body, re.I) and re.search(
            r'name=["\']password["\']', body, re.I
        ):
            # Login page itself — do not assert body_not_contains
            pass
        elif "/auth/login" not in url.lower():
            assertion["body_not_contains"] = [
                'name="username"',
                'name="password"',
            ]
        return assertion

    if body and len(body.strip()) >= 12 and not _HEAVY_DIGIT_RE.search(body[:80]):
        # Plain text: use a short prefix as presence check when stable
        snippet = body.strip()[:40]
        if not _DYNAMIC_TOKEN_RE.match(snippet):
            assertion["body_contains"] = [snippet]
    # else: status-only
    return assertion


def apply_txn_anchor_assertions(
    transactions: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    network_requests: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Mark one anchor request per protocol TXN and attach a content assertion."""
    network_requests = network_requests or []
    for txn in transactions or []:
        if (txn.get("mode") or "protocol") == "browser":
            continue
        reqs = list(txn.get("requests") or [])
        if not reqs:
            continue
        # Preserve prior assertions when re-applying without captures (heal path)
        prior: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for r in reqs:
            a = r.get("assertion")
            if isinstance(a, dict):
                prior[(str(r.get("method") or "GET").upper(), str(r.get("url") or ""))] = a
            r.pop("assertion_anchor", None)
            r.pop("assertion", None)
            r.pop("synthesized_assertion", None)
        idx = select_anchor_request_index(reqs, correlations)
        if idx is None or idx < 0 or idx >= len(reqs):
            continue
        anchor = reqs[idx]
        anchor["assertion_anchor"] = True
        key = (
            str(anchor.get("method") or "GET").upper(),
            str(anchor.get("url") or ""),
        )
        captured = None
        if network_requests:
            look_url = str(anchor.get("url") or "")
            if "${" in look_url:
                look_url = look_url.split("?")[0]
            captured = _lookup_request(
                network_requests,
                method=str(anchor.get("method") or "GET"),
                url=look_url,
                step_indices=txn.get("step_indices"),
            )
            if captured is None:
                method_u = str(anchor.get("method") or "GET").upper()
                for req in network_requests:
                    if (req.get("method") or "GET").upper() != method_u:
                        continue
                    if _urls_match_loose(
                        str(req.get("url") or ""), str(anchor.get("url") or "")
                    ):
                        captured = req
                        break
        if not network_requests and key in prior:
            assertion = prior[key]
        else:
            assertion = derive_request_assertion(
                anchor, captured=captured, correlations=correlations
            )
        anchor["assertion"] = assertion
        anchor["synthesized_assertion"] = True
        txn["requests"] = reqs
        txn["anchor_request_index"] = idx


def is_valid_assertion(assertion: Any) -> bool:
    """Return True when ``assertion`` has at least one usable content condition."""
    if not isinstance(assertion, dict) or not assertion:
        return False
    if assertion.get("require_auth_session"):
        return True
    statuses = assertion.get("expect_status")
    if isinstance(statuses, list) and len(statuses) > 0:
        return True
    for key in ("json_path_exists", "body_contains", "body_not_contains"):
        val = assertion.get(key)
        if isinstance(val, list) and len(val) > 0:
            return True
        if isinstance(val, str) and val.strip():
            return True
    return False


def validate_txn_assertions(
    ir: Dict[str, Any],
    *,
    script: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """Ensure each protocol TXN has ≥1 valid content assertion (N TXNs ⇒ ≥N asserts).

    Browser-mode TXNs are excluded from the count. When ``script`` is provided,
    also require that emitted ``assertion:`` occurrences are ≥ protocol TXN count.

    Returns:
        ``(ok, notes)`` — notes list issues when ``ok`` is False.
    """
    notes: List[str] = []
    protocol_txns = [
        t
        for t in (ir.get("transactions") or [])
        if isinstance(t, dict) and (t.get("mode") or "protocol") != "browser"
    ]
    n_txns = len(protocol_txns)
    if n_txns == 0:
        return True, notes

    valid_count = 0
    for txn in protocol_txns:
        name = str(txn.get("name") or "Txn")
        reqs = [r for r in (txn.get("requests") or []) if isinstance(r, dict)]
        anchor = next((r for r in reqs if r.get("assertion_anchor")), None)
        if anchor is None:
            anchor = next(
                (r for r in reqs if is_valid_assertion(r.get("assertion"))),
                None,
            )
        if anchor is None:
            notes.append(
                f"Txn `{name}`: missing content assertion (no assertion_anchor)."
            )
            continue
        if not is_valid_assertion(anchor.get("assertion")):
            notes.append(
                f"Txn `{name}`: assertion present but has no valid conditions."
            )
            continue
        valid_count += 1

    if valid_count < n_txns:
        notes.append(
            f"Assertion coverage {valid_count}/{n_txns} protocol TXN(s) — "
            f"need ≥{n_txns} valid assertion(s)."
        )

    if script is not None:
        emitted = script.count("assertion:")
        if emitted < n_txns:
            notes.append(
                f"Emitted script has {emitted} content assertion(s) for "
                f"{n_txns} protocol TXN(s) — regenerate after fixing IR."
            )

    ok = valid_count >= n_txns
    if script is not None and script.count("assertion:") < n_txns:
        ok = False
    return ok, notes


def ensure_txn_assertions(
    ir: Dict[str, Any],
    *,
    network_requests: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply anchor assertions when coverage is incomplete; return IR + notes."""
    ok, notes = validate_txn_assertions(ir)
    if ok:
        return ir, []
    apply_txn_anchor_assertions(
        ir.get("transactions") or [],
        ir.get("correlations") or [],
        network_requests,
    )
    return ir, [
        "Re-applied per-TXN content assertion anchors before k6 run.",
        *notes,
    ]


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
    vars_list: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Wire login CSRF + username/password on ``auth/validate``.

    Always replaces captured/stale ``_token`` literals with ``${csrf_token}``.
    Also replaces username/password with IR credential vars so artifact
    redaction (``***REDACTED***`` in captured POST bodies) cannot break login.
    """
    has_browser_login = any(
        (t.get("mode") == "browser" and "login" in str(t.get("name") or "").lower())
        or t.get("synthesized") == "browser_login"
        for t in (transactions or [])
    )
    if has_browser_login:
        return
    csrf_var = "csrf_token"
    user_var, pwd_var = _credential_vars(vars_list or [])
    login_url = ""
    validate_url = ""
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            url = str(r.get("url") or "")
            method = str(r.get("method") or "").upper()
            if method == "GET" and "/auth/login" in url.lower():
                login_url = url
            body = r.get("body")
            is_login_post = method == "POST" and (
                "/auth/validate" in url.lower()
                or (
                    isinstance(body, dict)
                    and "password" in {str(k).lower() for k in body}
                    and bool(
                        {str(k).lower() for k in body}
                        & {"username", "user", "email", "login"}
                    )
                )
            )
            if not is_login_post:
                continue
            validate_url = url or validate_url
            if not isinstance(body, dict):
                body = {}
            else:
                body = dict(body)
            # Always correlate — never ship a captured CSRF literal.
            body["_token"] = f"${{{csrf_var}}}"
            if user_var:
                if "username" in body or not any(
                    k in body for k in ("user", "email", "login")
                ):
                    body["username"] = f"${{{user_var}}}"
                for key in ("user", "email", "login"):
                    if key in body:
                        body[key] = f"${{{user_var}}}"
            if pwd_var:
                for key in ("password", "passwd", "pwd"):
                    if key in body or key == "password":
                        body[key if key in body else "password"] = f"${{{pwd_var}}}"
                        break
            # Never leave redaction sentinels in credential fields
            for key, val in list(body.items()):
                kl = str(key).lower()
                sval = str(val)
                if kl in ("password", "passwd", "pwd") and pwd_var:
                    if sval in ("***REDACTED***", "****", "") or not sval.startswith(
                        "${"
                    ):
                        body[key] = f"${{{pwd_var}}}"
                if (
                    kl in ("username", "user", "email", "login")
                    and user_var
                    and sval in ("***REDACTED***", "****")
                ):
                    body[key] = f"${{{user_var}}}"
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
    else:
        # Ensure existing csrf_token correlation targets auth/validate body._token
        for c in correlations:
            if str(c.get("var") or "") != csrf_var:
                continue
            ps = dict(c.get("pass") or {})
            if "body._token" not in str(ps.get("to_location") or ""):
                ps["to_location"] = "body._token"
                ps["to_request"] = validate_url
                c["pass"] = ps
            ex = dict(c.get("extract") or {})
            if not ex.get("from_request") and login_url:
                ex["from_request"] = login_url
                ex["from_location"] = f"body.regex:{CSRF_TOKEN_REGEX}"
                c["extract"] = ex


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
        "think_time_s": dict(DEFAULT_THINK_TIME_S),
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


def _ensure_create_resource_ids(
    transactions: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    *,
    network_requests: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Wire create-POST ``data.id`` into downstream ``/requests/{id}`` paths.

    Captured scripts hardcode the Run-1 claim/request id (e.g. ``/requests/8``).
    Replaying that literal after a new create yields 403/404 — a script bug, not
    an application fault. Per Grafana k6 correlation guidance we extract the
    JSON id from the create response and substitute every matching path segment.
    """
    notes: List[str] = []
    create_posts: List[Dict[str, Any]] = []
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            method = str(r.get("method") or "").upper()
            url = str(r.get("url") or "")
            if method != "POST":
                continue
            # POST .../employees/{emp}/requests  or POST .../requests (no trailing id)
            if re.search(r"/requests/?(\?|$)", url) and not re.search(
                r"/requests/\d+", url
            ):
                create_posts.append(r)
            elif re.search(r"/requests/?$", urlparse(url).path or ""):
                create_posts.append(r)

    if not create_posts:
        return notes

    # Prefer id observed in capture response bodies when available
    captured_ids: Set[str] = set()
    for req in network_requests or []:
        method = str(req.get("method") or "").upper()
        url = str(req.get("url") or "")
        if method != "POST" or "/requests" not in url.lower():
            continue
        if re.search(r"/requests/\d+", url):
            continue
        body = req.get("response_body") or ""
        if not body:
            continue
        try:
            data = json.loads(body) if isinstance(body, str) else body
            rid = None
            if isinstance(data, dict):
                inner = data.get("data")
                if isinstance(inner, dict) and inner.get("id") is not None:
                    rid = inner.get("id")
                elif data.get("id") is not None:
                    rid = data.get("id")
            if rid is not None:
                captured_ids.add(str(rid))
        except Exception:
            continue

    # Also harvest numeric ids already embedded in downstream URLs
    path_id_re = re.compile(r"/requests/(\d+)(?:/|\?|$)")
    ui_id_re = re.compile(r"/assignClaim/id/(\d+)(?:/|\?|$)")
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            url = str(r.get("url") or "")
            for m in path_id_re.finditer(url):
                captured_ids.add(m.group(1))
            for m in ui_id_re.finditer(url):
                captured_ids.add(m.group(1))

    if not captured_ids:
        # Still add extract even without known literal — emitter needs the var
        captured_ids = set()

    var = "requestId"
    create_url = str(create_posts[0].get("url") or "")
    # Ensure correlation extract exists
    has_corr = any(str(c.get("var") or "") == var for c in correlations)
    if not has_corr:
        correlations.append(
            {
                "var": var,
                "extract": {
                    "from_request": create_url,
                    "from_location": "body.$.data.id",
                    "from_step": create_posts[0].get("step_index"),
                },
                "pass": {
                    "to_request": "",
                    "to_location": "path.requests",
                    "to_step": None,
                },
                "correlation_type": "response_extract",
                "confidence": "high",
                "auto_cookie": False,
                "synthesized": True,
                "reason": "create-resource id for downstream /requests/{id} paths",
            }
        )
        notes.append(
            f"Added `{var}` extract from create POST (`body.$.data.id`) for path correlation."
        )
    else:
        for c in correlations:
            if str(c.get("var") or "") != var:
                continue
            ex = dict(c.get("extract") or {})
            if "data.id" not in str(ex.get("from_location") or ""):
                ex["from_location"] = "body.$.data.id"
                c["extract"] = ex
            if not ex.get("from_request"):
                ex["from_request"] = create_url
                c["extract"] = ex

    # Substitute hardcoded ids in all request URLs (+ Referer headers)
    replaced = 0
    header_hits = 0
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            url = str(r.get("url") or "")
            if url and f"${{{var}}}" not in url:
                new_url = url
                new_url = re.sub(
                    r"(/requests/)(\d+)(?=/|\?|$)",
                    rf"\1${{{var}}}",
                    new_url,
                )
                new_url = re.sub(
                    r"(/assignClaim/id/)(\d+)(?=/|\?|$)",
                    rf"\1${{{var}}}",
                    new_url,
                )
                if new_url != url:
                    r["url"] = new_url
                    replaced += 1
            headers = r.get("headers")
            if isinstance(headers, dict):
                new_headers = dict(headers)
                changed = False
                for hk, hv in list(new_headers.items()):
                    s = str(hv or "")
                    ns = re.sub(
                        r"(/requests/)(\d+)(?=/|\?|$)",
                        rf"\1${{{var}}}",
                        s,
                    )
                    ns = re.sub(
                        r"(/assignClaim/id/)(\d+)(?=/|\?|$)",
                        rf"\1${{{var}}}",
                        ns,
                    )
                    if ns != s:
                        new_headers[hk] = ns
                        changed = True
                        header_hits += 1
                if changed:
                    r["headers"] = new_headers
    if replaced:
        notes.append(
            f"Replaced hardcoded create-resource id in {replaced} request URL(s) with `${{{var}}}`."
        )
    if header_hits:
        notes.append(
            f"Replaced hardcoded create-resource id in {header_hits} header value(s)."
        )
    return notes


def build_load_test_ir(
    *,
    target_url: str,
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    network_requests: Optional[List[Dict[str, Any]]] = None,
    credentials: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build deterministic tool-agnostic Load-Test IR.

    Args:
        target_url: Journey target URL.
        parameterizable_candidates: Candidate user-fed values.
        dependencies: Extract-to-pass correlation edges.
        transactions: Analyzed transaction definitions.
        network_requests: Optional full captures used to recover bodies/headers.
        credentials: Optional username/password from Watch-Me / Jira / chat.

    Returns:
        Versioned mapping with ``target_url``, ``origin``, ``vars``,
        ``correlations``, and normalized ``transactions``.
    """
    network_requests = network_requests or []
    credentials = {
        str(k): str(v)
        for k, v in (credentials or {}).items()
        if v is not None and str(v).strip()
    }

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
        entry: Dict[str, Any] = {
            "name": name,
            "value": value,
            "source": cand.get("selector") or "",
            "is_credential": bool(cand.get("is_credential")),
            "propagations": cand.get("propagations") or [],
        }
        # Infer credential from selector/name when classifier missed it
        src_l = str(entry["source"]).lower()
        if not entry["is_credential"] and (
            name.lower() in ("password", "passwd", "pwd", "username", "user", "email")
            or "password" in src_l
            or 'name="username"' in src_l
            or "name='username'" in src_l
        ):
            entry["is_credential"] = True
        if entry["is_credential"]:
            from src.security.secrets import (
                env_name_for_credential,
                is_redacted_secret,
            )

            entry["from_env"] = env_name_for_credential(name)
            # Keep real values in IR by default (multi-app). Only wipe when
            # operator explicitly disables NFE_STORE_CREDENTIALS.
            from config.settings import settings

            if not settings.NFE_STORE_CREDENTIALS or is_redacted_secret(value):
                # Prefer empty over baking the redaction sentinel into IR
                if is_redacted_secret(value):
                    entry["value"] = ""
                if not settings.NFE_STORE_CREDENTIALS:
                    entry["value"] = ""
            # Restore from journey credentials when fill was wiped
            if is_redacted_secret(entry["value"]) and credentials:
                restored = credentials.get(name) or credentials.get(name.lower())
                if not restored and name.lower() in ("username", "user"):
                    restored = (
                        credentials.get("username")
                        or credentials.get("user")
                        or credentials.get("email")
                    )
                if not restored and name.lower() in ("password", "passwd", "pwd"):
                    restored = credentials.get("password") or credentials.get("passwd")
                if restored and not is_redacted_secret(restored):
                    entry["value"] = str(restored)
        vars_list.append(entry)
        if entry["value"] and entry["value"] not in vars_by_value:
            vars_by_value[entry["value"]] = name

    # Ensure login vars exist when credentials were provided explicitly
    from config.settings import settings as _settings
    from src.security.secrets import (
        env_name_for_credential,
        is_redacted_secret as _is_redacted,
    )

    for cred_key, var_name in (
        ("username", "username"),
        ("user", "username"),
        ("email", "username"),
        ("password", "password"),
        ("passwd", "password"),
    ):
        raw = credentials.get(cred_key)
        if not raw or _is_redacted(raw) or var_name in seen_vars:
            continue
        seen_vars.add(var_name)
        stored = str(raw) if _settings.NFE_STORE_CREDENTIALS else ""
        vars_list.append(
            {
                "name": var_name,
                "value": stored,
                "source": f"credentials.{cred_key}",
                "is_credential": True,
                "from_env": env_name_for_credential(var_name),
                "propagations": [],
            }
        )
        if stored and stored not in vars_by_value:
            vars_by_value[stored] = var_name

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
                "think_time_s": dict(DEFAULT_THINK_TIME_S),
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
    _ensure_auth_csrf(
        txn_list, corr_list, origin=origin, vars_list=vars_list
    )
    _ensure_create_resource_ids(
        txn_list,
        corr_list,
        network_requests=network_requests,
    )
    apply_txn_anchor_assertions(txn_list, corr_list, network_requests)

    return {
        "version": 1,
        "target_url": target_url or "",
        "origin": origin,
        "vars": vars_list,
        "correlations": corr_list,
        "transactions": txn_list,
    }
