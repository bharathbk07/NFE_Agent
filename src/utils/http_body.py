"""Parse HTTP bodies, headers, and cookies for correlation and IR building."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs


def parse_post_data(
    raw: Any,
    content_type: str = "",
) -> Tuple[Any, str]:
    """Normalize an HTTP request body and classify its representation.

    Args:
        raw: Body as a mapping, sequence, string, scalar, or ``None``.
        content_type: Optional HTTP Content-Type header value.

    Returns:
        Pair of parsed body data and one of ``json``, ``form``, ``text``, or
        ``empty``.
    """
    if raw is None or raw == "":
        return None, "empty"

    if isinstance(raw, (dict, list)):
        return raw, "json"

    text = str(raw)
    ct = (content_type or "").lower()

    if "json" in ct or text[:1] in ("{", "["):
        try:
            return json.loads(text), "json"
        except Exception:
            pass

    if (
        "application/x-www-form-urlencoded" in ct
        or ("=" in text and "&" in text)
        or (text.count("=") == 1 and "&" not in text and " " not in text.strip())
    ):
        try:
            parsed = parse_qs(text, keep_blank_values=True)
            # flatten single-value lists for easier correlation/IR
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in parsed.items()}
            if flat:
                return flat, "form"
        except Exception:
            pass

    return text, "text"


def content_type_from_headers(headers: Optional[Dict[str, Any]]) -> str:
    """Read a Content-Type value case-insensitively.

    Args:
        headers: Optional HTTP header mapping.

    Returns:
        Header value as text, or an empty string when absent.
    """
    if not headers:
        return ""
    for k, v in headers.items():
        if str(k).lower() == "content-type":
            return str(v or "")
    return ""


def flatten_body_fields(body: Any, prefix: str = "") -> Dict[str, str]:
    """Flatten nested body leaves into paths for parameter matching.

    Args:
        body: Nested dictionaries, lists, or scalar body data.
        prefix: Existing path prefix used during recursion.

    Returns:
        Mapping of dotted/indexed field paths to string leaf values.
    """
    out: Dict[str, str] = {}
    if isinstance(body, dict):
        for k, v in body.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(flatten_body_fields(v, path))
            elif v is not None:
                out[path] = str(v)
    elif isinstance(body, list):
        for i, v in enumerate(body):
            path = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(flatten_body_fields(v, path))
            elif v is not None:
                out[path] = str(v)
    elif body is not None and prefix:
        out[prefix] = str(body)
    return out


def parse_set_cookie_pairs(header_val: str) -> Dict[str, str]:
    """Parse the leading cookie pair from one Set-Cookie value.

    Args:
        header_val: Raw Set-Cookie header text.

    Returns:
        Zero- or one-item mapping from cookie name to value.
    """
    cookies: Dict[str, str] = {}
    if not header_val:
        return cookies
    # Multiple Set-Cookie may be joined; take first segment before attributes
    first = header_val.split(";")[0].strip()
    if "=" in first:
        name, _, value = first.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


_COOKIE_NAME_RE = re.compile(r"([^=;\s]+)\s*=")


def cookie_names_from_set_cookie(headers: Dict[str, Any]) -> Dict[str, str]:
    """Collect cookies from response Set-Cookie headers.

    Args:
        headers: Response header mapping, potentially containing joined
            Set-Cookie values.

    Returns:
        Mapping from discovered cookie names to values.
    """
    found: Dict[str, str] = {}
    if not headers:
        return found
    for k, v in headers.items():
        if str(k).lower() != "set-cookie":
            continue
        # Playwright/CDP may give one string; sometimes newline-joined
        for part in re.split(r"[\n,]", str(v)):
            # Only treat as new cookie if it looks like name=value at start
            part = part.strip()
            if not part or "=" not in part:
                continue
            # Skip if this looks like an attribute-only fragment
            name = part.split("=", 1)[0].strip().lower()
            if name in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
                continue
            found.update(parse_set_cookie_pairs(part))
    return found
