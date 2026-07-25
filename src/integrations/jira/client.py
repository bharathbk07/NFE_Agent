"""Jira Cloud REST client (source of truth for the NFE worker)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

import httpx

from config.settings import settings
from src.exceptions import (
    ErrorCode,
    NFEAuthError,
    NFEConfigError,
    NFEIntegrationError,
)
from src.integrations.jira.security import sanitize_comment

logger = logging.getLogger(__name__)


class JiraAPIError(NFEIntegrationError):
    """Raised when a Jira REST call fails with a user-facing explanation."""

    default_code = ErrorCode.JIRA_API
    default_user_message = "A Jira API call failed."

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        issue_key: str = "",
        code: Optional[str] = None,
        user_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        resolved_code = code
        if resolved_code is None:
            if status_code in (401, 403):
                resolved_code = ErrorCode.JIRA_AUTH
            elif status_code == 404:
                resolved_code = ErrorCode.JIRA_NOT_FOUND
            else:
                resolved_code = ErrorCode.JIRA_API
        detail_map = dict(details or {})
        if status_code is not None:
            detail_map.setdefault("status_code", status_code)
        if issue_key:
            detail_map.setdefault("issue_key", issue_key)
        super().__init__(
            message,
            code=resolved_code,
            user_message=user_message or message,
            details=detail_map,
            cause=cause,
        )
        self.status_code = status_code
        self.issue_key = issue_key


@dataclass
class JiraIssue:
    """Normalized issue fields used by the NFE worker."""

    key: str
    summary: str = ""
    description: str = ""
    acceptance_criteria: str = ""
    labels: List[str] = field(default_factory=list)
    issue_type: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def has_label(self, label: str) -> bool:
        """True if any issue label matches ``label`` (ignores trailing commas/spaces)."""
        want = _normalize_label(label)
        if not want:
            return False
        return any(_normalize_label(x) == want for x in (self.labels or []))


def _normalize_label(label: str) -> str:
    """Strip whitespace and a trailing comma (common paste mistake in Jira UI)."""
    return (label or "").strip().rstrip(",").strip()


def _adf_to_text(node: Any) -> str:
    """Best-effort plain text from Atlassian Document Format or string."""
    from src.integrations.jira.adf import adf_to_text

    return adf_to_text(node)


def _error_body(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "")[:300]
    msgs = data.get("errorMessages") or []
    if isinstance(msgs, list) and msgs:
        return "; ".join(str(m) for m in msgs)
    errors = data.get("errors") or {}
    if errors:
        return str(errors)
    return (resp.text or "")[:300]


def _raise_for_status(resp: httpx.Response, *, context: str, issue_key: str = "") -> None:
    """Translate HTTP errors into typed Jira / auth exceptions."""
    if resp.is_success:
        return
    detail = _error_body(resp)
    code = resp.status_code
    key_hint = f" ({issue_key})" if issue_key else ""

    if code in (401, 403):
        raise NFEAuthError(
            f"Jira authentication/permission failed for {context}{key_hint} "
            f"(HTTP {code}): {detail}. "
            "Check JIRA_EMAIL + JIRA_API_TOKEN (same Atlassian account that can "
            "open the issue in the browser). Recreate the token at "
            "https://id.atlassian.com/manage-profile/security/api-tokens",
            code=ErrorCode.JIRA_AUTH,
            user_message=(
                "Jira authentication failed. Check JIRA_EMAIL and JIRA_API_TOKEN."
            ),
            details={"status_code": code, "issue_key": issue_key, "context": context},
        )
    if code == 404:
        raise JiraAPIError(
            f"Jira could not find {context}{key_hint} (HTTP 404): {detail}. "
            "Either the key is wrong, JIRA_BASE_URL points at a different site, "
            "or the API token account cannot see that project "
            "(Jira often returns 404 instead of 403).",
            status_code=code,
            issue_key=issue_key,
            code=ErrorCode.JIRA_NOT_FOUND,
            user_message=f"Jira could not find {context}{key_hint}.",
        )
    if code == 410:
        raise JiraAPIError(
            f"Jira API endpoint gone for {context} (HTTP 410): {detail}. "
            "Update the NFE Jira client or check Atlassian API changelog.",
            status_code=code,
            issue_key=issue_key,
        )
    raise JiraAPIError(
        f"Jira request failed for {context}{key_hint} (HTTP {code}): {detail}",
        status_code=code,
        issue_key=issue_key,
    )


class JiraClient:
    """Thin Jira Cloud REST v3 client using email + API token."""

    _ac_field_cache: Optional[str] = None
    _ac_field_resolved: bool = False

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or settings.JIRA_BASE_URL).rstrip("/")
        self.email = (email if email is not None else settings.JIRA_EMAIL).strip()
        self.api_token = (
            api_token if api_token is not None else settings.JIRA_API_TOKEN
        ).strip()
        self.timeout = timeout
        if not self.base_url or not self.email or not self.api_token:
            raise NFEConfigError(
                "Jira client requires JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN",
                code=ErrorCode.CONFIG_MISSING,
                user_message=(
                    "Jira is not configured. Set JIRA_BASE_URL, JIRA_EMAIL, "
                    "and JIRA_API_TOKEN."
                ),
            )

    def _client(self) -> httpx.Client:
        # trust_env=False avoids corporate HTTP_PROXY breaking Atlassian calls.
        return httpx.Client(
            base_url=self.base_url,
            auth=(self.email, self.api_token),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=self.timeout,
            trust_env=False,
        )

    def resolve_acceptance_field(self) -> str:
        """Return configured or auto-discovered Acceptance Criteria field id."""
        configured = (settings.NFE_JIRA_ACCEPTANCE_FIELD or "").strip()
        if configured:
            return configured
        if JiraClient._ac_field_resolved:
            return JiraClient._ac_field_cache or ""
        JiraClient._ac_field_resolved = True
        try:
            with self._client() as client:
                resp = client.get("/rest/api/3/field")
                if not resp.is_success:
                    return ""
                for item in resp.json() or []:
                    name = str(item.get("name") or "").strip().lower()
                    if name in ("acceptance criteria", "acceptance criterion"):
                        fid = str(item.get("id") or "").strip()
                        JiraClient._ac_field_cache = fid
                        logger.info("Auto-discovered acceptance field: %s", fid)
                        return fid
        except Exception as exc:
            logger.debug("Could not discover acceptance field: %s", exc)
        return ""

    def verify_auth(self) -> Dict[str, Any]:
        """Call ``/myself`` to confirm API token auth works."""
        with self._client() as client:
            resp = client.get("/rest/api/3/myself")
            _raise_for_status(resp, context="GET /myself")
            return resp.json()

    def get_issue(self, key: str) -> JiraIssue:
        """Fetch an issue and normalize description / labels / AC."""
        fields = [
            "summary",
            "description",
            "labels",
            "issuetype",
            "status",
        ]
        ac_field = self.resolve_acceptance_field()
        if ac_field:
            fields.append(ac_field)
        with self._client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}",
                params={"fields": ",".join(fields)},
            )
            _raise_for_status(resp, context=f"GET issue {key}", issue_key=key)
            data = resp.json()
        f = data.get("fields") or {}
        ac_text = ""
        if ac_field and ac_field in f and f.get(ac_field) is not None:
            ac_text = _adf_to_text(f.get(ac_field))
        it = f.get("issuetype") or {}
        st = f.get("status") or {}
        return JiraIssue(
            key=data.get("key") or key,
            summary=str(f.get("summary") or ""),
            description=_adf_to_text(f.get("description")),
            acceptance_criteria=ac_text,
            labels=list(f.get("labels") or []),
            issue_type=str(it.get("name") or ""),
            status=str(st.get("name") or ""),
            raw=data,
        )

    def search_jql(self, jql: str, *, max_results: int = 20) -> List[JiraIssue]:
        """Search issues by JQL via the current Cloud ``/search/jql`` API."""
        with self._client() as client:
            # Legacy GET /rest/api/3/search returns 410 Gone on many Cloud sites.
            resp = client.post(
                "/rest/api/3/search/jql",
                json={
                    "jql": jql,
                    "maxResults": max_results,
                    "fields": ["summary", "labels", "issuetype", "status"],
                },
            )
            _raise_for_status(resp, context="POST /search/jql")
            data = resp.json()
        issues = []
        for item in data.get("issues") or []:
            key = item.get("key")
            if not key:
                continue
            f = item.get("fields") or {}
            it = f.get("issuetype") or {}
            st = f.get("status") or {}
            issues.append(
                JiraIssue(
                    key=key,
                    summary=str(f.get("summary") or ""),
                    labels=list(f.get("labels") or []),
                    issue_type=str(it.get("name") or ""),
                    status=str(st.get("name") or ""),
                    raw=item,
                )
            )
        return issues

    def add_comment(self, key: str, body: Union[str, Dict[str, Any]]) -> None:
        """Add a comment using Jira Cloud ADF (not wiki/Markdown).

        Args:
            key: Issue key.
            body: Either a lightweight report string (converted to ADF) or a
                ready ADF ``doc`` object.
        """
        from src.integrations.jira.adf import report_markup_to_adf

        if isinstance(body, dict) and body.get("type") == "doc":
            adf_body = body
        else:
            text = sanitize_comment(str(body or ""))
            adf_body = report_markup_to_adf(text)
        payload = {"body": adf_body}
        with self._client() as client:
            resp = client.post(f"/rest/api/3/issue/{key}/comment", json=payload)
            _raise_for_status(resp, context=f"POST comment {key}", issue_key=key)

    def add_labels(self, key: str, labels: Sequence[str]) -> None:
        """Add labels without removing existing ones."""
        updates = [{"add": lab} for lab in labels if lab]
        if not updates:
            return
        with self._client() as client:
            resp = client.put(
                f"/rest/api/3/issue/{key}",
                json={"update": {"labels": updates}},
            )
            _raise_for_status(resp, context=f"PUT labels add {key}", issue_key=key)

    def remove_labels(self, key: str, labels: Sequence[str]) -> None:
        """Remove labels if present."""
        updates = [{"remove": lab} for lab in labels if lab]
        if not updates:
            return
        with self._client() as client:
            resp = client.put(
                f"/rest/api/3/issue/{key}",
                json={"update": {"labels": updates}},
            )
            _raise_for_status(resp, context=f"PUT labels remove {key}", issue_key=key)

    def set_lifecycle(
        self,
        key: str,
        *,
        add: Optional[Sequence[str]] = None,
        remove: Optional[Sequence[str]] = None,
    ) -> None:
        """Add and remove labels in one update."""
        ops = []
        for lab in add or []:
            ops.append({"add": lab})
        for lab in remove or []:
            ops.append({"remove": lab})
        if not ops:
            return
        with self._client() as client:
            resp = client.put(
                f"/rest/api/3/issue/{key}",
                json={"update": {"labels": ops}},
            )
            _raise_for_status(resp, context=f"PUT lifecycle {key}", issue_key=key)

    def list_comments(self, key: str, *, max_results: int = 50) -> List[str]:
        """Return plain-text comment bodies (oldest → newest)."""
        with self._client() as client:
            resp = client.get(
                f"/rest/api/3/issue/{key}/comment",
                params={"maxResults": max_results, "orderBy": "created"},
            )
            _raise_for_status(resp, context=f"GET comments {key}", issue_key=key)
            data = resp.json()
        texts: List[str] = []
        for item in data.get("comments") or []:
            texts.append(_adf_to_text(item.get("body")).strip())
        return [t for t in texts if t]

    def transition_to_status(self, key: str, target_status: str) -> bool:
        """Transition issue to a status by destination (or transition) name.

        Returns:
            ``True`` when a matching transition was applied.
        """
        want = (target_status or "").strip().lower()
        if not want:
            return False
        with self._client() as client:
            resp = client.get(f"/rest/api/3/issue/{key}/transitions")
            _raise_for_status(
                resp, context=f"GET transitions {key}", issue_key=key
            )
            transitions = (resp.json() or {}).get("transitions") or []
            match_id = None
            for tr in transitions:
                to_name = str(((tr.get("to") or {}).get("name") or "")).strip().lower()
                tr_name = str(tr.get("name") or "").strip().lower()
                if to_name == want or tr_name == want:
                    match_id = tr.get("id")
                    break
            if not match_id:
                logger.warning(
                    "No transition to %r for %s (available: %s)",
                    target_status,
                    key,
                    [
                        f"{t.get('name')}→{(t.get('to') or {}).get('name')}"
                        for t in transitions
                    ],
                )
                return False
            resp2 = client.post(
                f"/rest/api/3/issue/{key}/transitions",
                json={"transition": {"id": str(match_id)}},
            )
            _raise_for_status(
                resp2, context=f"POST transition {key}→{target_status}", issue_key=key
            )
            return True
