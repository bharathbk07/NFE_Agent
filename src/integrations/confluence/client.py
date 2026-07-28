"""Confluence Cloud REST client (page create/update + attachments)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from config.settings import settings
from src.exceptions import ErrorCode, NFEAuthError, NFEConfigError, NFEIntegrationError

logger = logging.getLogger(__name__)


class ConfluenceAPIError(NFEIntegrationError):
    """Raised when a Confluence REST call fails."""

    default_code = ErrorCode.INTEGRATION
    default_user_message = "A Confluence API call failed."

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        code: Optional[str] = None,
        user_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        detail_map = dict(details or {})
        if status_code is not None:
            detail_map.setdefault("status_code", status_code)
        super().__init__(
            message,
            code=code or ErrorCode.INTEGRATION,
            user_message=user_message or message,
            details=detail_map,
            cause=cause,
        )
        self.status_code = status_code


def _error_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    msg = data.get("message") or data.get("errorMessage")
    if msg:
        return str(msg)
    msgs = data.get("errorMessages") or []
    if isinstance(msgs, list) and msgs:
        return "; ".join(str(m) for m in msgs)
    return (resp.text or "")[:300]


def _raise_for_status(resp: httpx.Response, *, context: str) -> None:
    if resp.is_success:
        return
    detail = _error_body(resp)
    code = resp.status_code
    if code in (401, 403):
        raise NFEAuthError(
            f"Confluence auth failed during {context}: {detail}",
            code=ErrorCode.AUTH,
            user_message="Confluence authentication failed. Check email/API token and space permissions.",
            details={"status_code": code, "context": context},
        )
    raise ConfluenceAPIError(
        f"Confluence {context} failed ({code}): {detail}",
        status_code=code,
        details={"context": context},
    )


class ConfluenceClient:
    """Minimal Confluence Cloud REST helper using Basic auth."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        space_key: Optional[str] = None,
        timeout_s: float = 60.0,
    ) -> None:
        resolved_base = (
            (base_url or settings.CONFLUENCE_BASE_URL or settings.JIRA_BASE_URL or "")
            .strip()
            .rstrip("/")
        )
        resolved_email = (email or settings.CONFLUENCE_EMAIL or settings.JIRA_EMAIL or "").strip()
        resolved_token = (
            api_token or settings.CONFLUENCE_API_TOKEN or settings.JIRA_API_TOKEN or ""
        ).strip()
        resolved_space = (space_key or settings.CONFLUENCE_SPACE_KEY or "").strip()

        if not resolved_base:
            raise NFEConfigError(
                "CONFLUENCE_BASE_URL (or JIRA_BASE_URL) is not set",
                user_message="Confluence base URL is not configured.",
            )
        if not resolved_email or not resolved_token:
            raise NFEConfigError(
                "Confluence credentials missing",
                user_message="Confluence email/API token (or JIRA_*) are not configured.",
            )
        if not resolved_space:
            raise NFEConfigError(
                "CONFLUENCE_SPACE_KEY is not set",
                user_message="Confluence space key is not configured.",
            )

        self.base_url = resolved_base
        self.email = resolved_email
        self.api_token = resolved_token
        self.space_key = resolved_space
        self.timeout_s = timeout_s
        self._api = f"{self.base_url}/wiki/rest/api"

    def _client(self) -> httpx.Client:
        return httpx.Client(
            auth=(self.email, self.api_token),
            timeout=self.timeout_s,
            headers={"Accept": "application/json"},
        )

    def page_url(self, page_id: str) -> str:
        """Browser URL for a content id."""
        return f"{self.base_url}/wiki/spaces/{quote(self.space_key)}/pages/{page_id}"

    def find_page_by_title(
        self,
        title: str,
        *,
        parent_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return the first page in the space matching ``title`` (optionally under parent)."""
        cql_title = title.replace("\\", "\\\\").replace('"', '\\"')
        cql = f'space="{self.space_key}" AND type=page AND title="{cql_title}"'
        if parent_id:
            cql += f" AND parent={parent_id}"
        with self._client() as client:
            resp = client.get(
                f"{self._api}/content/search",
                params={"cql": cql, "limit": 5, "expand": "version,ancestors"},
            )
            _raise_for_status(resp, context="content search")
            results = (resp.json() or {}).get("results") or []
        if not results:
            # Fallback: title query without CQL parent (older sites)
            with self._client() as client:
                resp = client.get(
                    f"{self._api}/content",
                    params={
                        "spaceKey": self.space_key,
                        "title": title,
                        "type": "page",
                        "expand": "version,ancestors",
                        "limit": 10,
                    },
                )
                _raise_for_status(resp, context="content by title")
                results = (resp.json() or {}).get("results") or []
            if parent_id:
                filtered: List[Dict[str, Any]] = []
                for page in results:
                    ancestors = page.get("ancestors") or []
                    if any(str(a.get("id")) == str(parent_id) for a in ancestors):
                        filtered.append(page)
                    elif ancestors and str((ancestors[-1] or {}).get("id")) == str(parent_id):
                        filtered.append(page)
                results = filtered or results
        return results[0] if results else None

    def create_page(
        self,
        *,
        title: str,
        storage_body: str,
        parent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a page under the space (optionally as a child of ``parent_id``)."""
        payload: Dict[str, Any] = {
            "type": "page",
            "title": title,
            "space": {"key": self.space_key},
            "body": {
                "storage": {
                    "value": storage_body,
                    "representation": "storage",
                }
            },
        }
        if parent_id:
            payload["ancestors"] = [{"id": str(parent_id)}]
        with self._client() as client:
            resp = client.post(f"{self._api}/content", json=payload)
            _raise_for_status(resp, context="create page")
            return resp.json()

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        storage_body: str,
        version_number: int,
    ) -> Dict[str, Any]:
        """Update page body/title (version_number is the *current* version)."""
        payload = {
            "id": str(page_id),
            "type": "page",
            "title": title,
            "space": {"key": self.space_key},
            "body": {
                "storage": {
                    "value": storage_body,
                    "representation": "storage",
                }
            },
            "version": {
                "number": int(version_number) + 1,
                "message": "NFE Agent results update",
            },
        }
        with self._client() as client:
            resp = client.put(f"{self._api}/content/{page_id}", json=payload)
            _raise_for_status(resp, context="update page")
            return resp.json()

    def get_page(self, page_id: str, *, expand: str = "version,body.storage") -> Dict[str, Any]:
        with self._client() as client:
            resp = client.get(
                f"{self._api}/content/{page_id}",
                params={"expand": expand},
            )
            _raise_for_status(resp, context="get page")
            return resp.json()

    def upload_attachment(
        self,
        *,
        page_id: str,
        file_path: str,
        filename: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload or replace an attachment on ``page_id``."""
        path = Path(file_path)
        if not path.is_file():
            raise ConfluenceAPIError(
                f"Attachment file not found: {file_path}",
                user_message="A Confluence attachment file was missing on disk.",
            )
        name = filename or path.name
        headers = {
            "X-Atlassian-Token": "no-check",
            "Accept": "application/json",
        }
        with self._client() as client:
            with path.open("rb") as fh:
                files = {"file": (name, fh)}
                resp = client.post(
                    f"{self._api}/content/{page_id}/child/attachment",
                    headers=headers,
                    files=files,
                )
            # If attachment exists, Confluence may 400 — try update endpoint
            if resp.status_code == 400 and "already exists" in (resp.text or "").lower():
                resp = client.post(
                    f"{self._api}/content/{page_id}/child/attachment/{quote(name)}/data",
                    headers=headers,
                    files={"file": (name, path.read_bytes())},
                )
            _raise_for_status(resp, context="upload attachment")
            data = resp.json() or {}
            results = data.get("results") or []
            if results:
                return results[0]
            return data

    def attachment_download_url(self, page_id: str, filename: str) -> str:
        return (
            f"{self.base_url}/wiki/download/attachments/{page_id}/"
            f"{quote(filename)}"
        )
