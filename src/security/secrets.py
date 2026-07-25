"""Credential placeholders for LLMs and redaction for logs/artifacts."""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from config.settings import settings

_REDACTED = "***REDACTED***"

_SENSITIVE_HEADER_KEYS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-csrf-token",
        "x-access-token",
    }
)

_PASSWORD_KEYS = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "client_secret",
    }
)

_CRED_PLACEHOLDER_RE = re.compile(r"\$\{cred:([A-Za-z_][A-Za-z0-9_]*)\}")
_PASSWORD_KV_RE = re.compile(
    r'(?i)((?:password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*)([^\s&"\']+)'
)
_PASSWORD_JSON_RE = re.compile(
    r'(?i)("(?:password|passwd|pwd|secret|token|api[_-]?key)"\s*:\s*")([^"]*)(")'
)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._\-+/=]{8,})")


def credentials_placeholders(credentials: Optional[Mapping[str, str]]) -> Dict[str, str]:
    """Map credential keys to ``${cred:key}`` placeholders for LLM prompts."""
    return {str(k): f"${{cred:{k}}}" for k in (credentials or {})}


def substitute_credential_placeholders(
    steps: Iterable[Mapping[str, Any]],
    credentials: Optional[Mapping[str, str]],
) -> List[Dict[str, Any]]:
    """Replace ``${cred:name}`` placeholders in step values with real secrets."""
    creds = dict(credentials or {})

    def _sub(text: Any) -> Any:
        if not isinstance(text, str) or "${cred:" not in text:
            return text

        def repl(m: re.Match[str]) -> str:
            key = m.group(1)
            return str(creds.get(key, m.group(0)))

        return _CRED_PLACEHOLDER_RE.sub(repl, text)

    out: List[Dict[str, Any]] = []
    for step in steps or []:
        item = dict(step)
        if "value" in item:
            item["value"] = _sub(item.get("value"))
        if "url" in item and isinstance(item.get("url"), str):
            item["url"] = _sub(item["url"])
        out.append(item)
    return out


def redact_text_for_llm(text: str) -> str:
    """Mask password-like assignments before sending text to an LLM."""
    s = text or ""
    s = _PASSWORD_JSON_RE.sub(rf"\1{_REDACTED}\3", s)
    s = _PASSWORD_KV_RE.sub(rf"\1{_REDACTED}", s)
    s = _BEARER_RE.sub(rf"\1{_REDACTED}", s)
    return s


def _mask_headers(headers: Any) -> Any:
    if not isinstance(headers, dict):
        return headers
    out = {}
    for k, v in headers.items():
        if str(k).lower() in _SENSITIVE_HEADER_KEYS:
            out[k] = _REDACTED
        else:
            out[k] = v
    return out


def _mask_mapping_values(
    body: Any,
    *,
    known_secrets: Optional[Iterable[str]] = None,
) -> Any:
    secrets = {str(s) for s in (known_secrets or []) if s}
    if isinstance(body, dict):
        out = {}
        for k, v in body.items():
            key_l = str(k).lower()
            if key_l in _PASSWORD_KEYS or key_l.endswith("password"):
                out[k] = _REDACTED
            elif isinstance(v, str) and v and v in secrets:
                out[k] = _REDACTED
            else:
                out[k] = _mask_mapping_values(v, known_secrets=secrets)
        return out
    if isinstance(body, list):
        return [_mask_mapping_values(x, known_secrets=secrets) for x in body]
    if isinstance(body, str) and body and body in secrets:
        return _REDACTED
    return body


def redact_network_request(
    req: Mapping[str, Any],
    *,
    known_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return a shallow-copied request with sensitive headers/body fields masked."""
    out = copy.deepcopy(dict(req))
    if "headers" in out:
        out["headers"] = _mask_headers(out.get("headers"))
    if "request_headers" in out:
        out["request_headers"] = _mask_headers(out.get("request_headers"))
    if "response_headers" in out:
        out["response_headers"] = _mask_headers(out.get("response_headers"))
    for key in ("post_data", "body", "request_body", "response_body"):
        if key in out:
            val = out[key]
            if isinstance(val, (dict, list)):
                out[key] = _mask_mapping_values(val, known_secrets=known_secrets)
            elif isinstance(val, str):
                masked = redact_text_for_llm(val)
                secrets = {str(s) for s in (known_secrets or []) if s}
                for secret in secrets:
                    if secret and secret in masked:
                        masked = masked.replace(secret, _REDACTED)
                out[key] = masked
    return out


def redact_step(
    step: Mapping[str, Any],
    *,
    known_secrets: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Mask fill/select values that look like secrets."""
    out = dict(step)
    action = str(out.get("action") or "").lower()
    value = out.get("value")
    secrets = {str(s) for s in (known_secrets or []) if s}
    selector = str(out.get("selector") or "").lower()
    if action in ("fill", "select") and value is not None:
        if "password" in selector or (isinstance(value, str) and value in secrets):
            out["value"] = _REDACTED
    return out


def redact_run_records(
    records: Iterable[Mapping[str, Any]],
    *,
    known_secrets: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Deep-redact network captures inside run records."""
    out: List[Dict[str, Any]] = []
    for rec in records or []:
        item = copy.deepcopy(dict(rec))
        reqs = item.get("network_requests") or []
        if isinstance(reqs, list):
            item["network_requests"] = [
                redact_network_request(r, known_secrets=known_secrets)
                if isinstance(r, dict)
                else r
                for r in reqs
            ]
        cookies = item.get("cookies")
        if isinstance(cookies, list) and settings.NFE_REDACT_ARTIFACTS:
            item["cookies"] = [
                {**c, "value": _REDACTED} if isinstance(c, dict) else c for c in cookies
            ]
        for store_key in ("local_storage", "session_storage"):
            store = item.get(store_key)
            if isinstance(store, dict) and settings.NFE_REDACT_ARTIFACTS:
                item[store_key] = {k: _REDACTED for k in store}
        out.append(item)
    return out


def credentials_for_storage(
    credentials: Optional[Mapping[str, str]],
) -> Dict[str, str]:
    """Return credentials safe to persist (keys only unless store enabled)."""
    creds = dict(credentials or {})
    if settings.NFE_STORE_CREDENTIALS:
        return creds
    return {k: "" for k in creds}


def env_name_for_credential(var_name: str) -> str:
    """Map an IR credential var name to a k6 ``__ENV`` key."""
    n = (var_name or "").lower()
    if n in ("username", "user", "email", "login"):
        return "NFE_USER"
    if n in ("password", "passwd", "pwd"):
        return "NFE_PASS"
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", var_name or "VALUE").upper()
    return f"NFE_CRED_{cleaned}"
