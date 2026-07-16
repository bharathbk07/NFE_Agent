"""Convert CDP or Playwright network captures into HAR 1.2 documents."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlparse


def _headers_list(headers: Dict[str, Any]) -> List[Dict[str, str]]:
    """Convert a header mapping to HAR name/value objects.

    Args:
        headers: Header names mapped to arbitrary scalar values.

    Returns:
        List of ``{"name": str, "value": str}`` dictionaries.
    """
    return [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()]


def _query_string(url: str) -> List[Dict[str, str]]:
    """Parse URL query parameters into HAR name/value objects.

    Args:
        url: URL whose query string should be parsed.

    Returns:
        Ordered list of query parameter dictionaries, or an empty list when
        parsing fails.
    """
    try:
        return [{"name": k, "value": v} for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    except Exception:
        return []


def _post_data_block(post_data: Any) -> Dict[str, Any] | None:
    """Create a bounded HAR ``postData`` block.

    Args:
        post_data: Request body as JSON-compatible data or text.

    Returns:
        HAR body mapping with MIME type and at most 200,000 text characters,
        or ``None`` for an empty body.
    """
    if post_data is None or post_data == "":
        return None
    if isinstance(post_data, (dict, list)):
        text = json.dumps(post_data)
        mime = "application/json"
    else:
        text = str(post_data)
        mime = "text/plain"
    return {"mimeType": mime, "text": text[:200_000]}


def network_logs_to_har(
    network_requests: List[Dict[str, Any]],
    *,
    creator_name: str = "nfe-agent",
    creator_version: str = "1.0",
) -> Dict[str, Any]:
    """Convert recorder requests into a HAR 1.2 document.

    Args:
        network_requests: Captured request dictionaries with request and
            optional response metadata.
        creator_name: Name placed in the HAR creator block.
        creator_version: Version placed in the HAR creator block.

    Returns:
        HAR mapping shaped as ``{"log": {"version", "creator", "pages",
        "entries"}}`` for import or offline analysis.
    """
    entries = []
    for req in network_requests:
        url = req.get("url") or ""
        started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        post = _post_data_block(req.get("post_data"))
        request_block: Dict[str, Any] = {
            "method": req.get("method") or "GET",
            "url": url,
            "httpVersion": "HTTP/1.1",
            "headers": _headers_list(req.get("headers") or {}),
            "queryString": _query_string(url),
            "cookies": [],
            "headersSize": -1,
            "bodySize": -1,
        }
        if post:
            request_block["postData"] = post

        response_block = {
            "status": int(req.get("status") or 0),
            "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": _headers_list(req.get("response_headers") or {}),
            "cookies": [],
            "content": {
                "size": len(str(req.get("response_body") or "")),
                "mimeType": req.get("mime_type") or "",
                "text": str(req.get("response_body") or "")[:200_000],
            },
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": -1,
        }

        entries.append({
            "startedDateTime": started,
            "time": 0,
            "request": request_block,
            "response": response_block,
            "cache": {},
            "timings": {"send": 0, "wait": 0, "receive": 0},
            "_step_index": req.get("step_index"),
            "_step_action": req.get("step_action"),
            "_resource_type": req.get("resource_type"),
            "_capture_source": req.get("capture_source"),
        })

    return {
        "log": {
            "version": "1.2",
            "creator": {"name": creator_name, "version": creator_version},
            "pages": [],
            "entries": entries,
        }
    }
