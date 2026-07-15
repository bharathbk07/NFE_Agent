"""Build a HAR 1.2-compatible dict from captured network logs (CDP / Playwright)."""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List
from urllib.parse import parse_qsl, urlparse


def _headers_list(headers: Dict[str, Any]) -> List[Dict[str, str]]:
    return [{"name": str(k), "value": str(v)} for k, v in (headers or {}).items()]


def _query_string(url: str) -> List[Dict[str, str]]:
    try:
        return [{"name": k, "value": v} for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)]
    except Exception:
        return []


def _post_data_block(post_data: Any) -> Dict[str, Any] | None:
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
    """
    Convert recorder network_requests into a HAR 1.2 document.
    Useful for JMeter/BlazeMeter import and offline DevTools analysis.
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
