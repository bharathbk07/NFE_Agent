"""Parse Jira story description / AC into a performance request."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

from src.security.secrets import redact_text_for_llm
from src.security.url_policy import UrlPolicyError, assert_url_allowed

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(
    r"```(?:ya?ml|json)?\s*\n(.*?)```",
    re.IGNORECASE | re.DOTALL,
)
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_VUS_RE = re.compile(
    r"\b(\d+)\s*(?:virtual\s+users?|vus?|users?)\b",
    re.IGNORECASE,
)
_RECORDING_RE = re.compile(
    r"\b(?:recording|watch[\s-]?me)\b[:\s]+[\"']?([^\"'\n]+)[\"']?",
    re.IGNORECASE,
)


@dataclass
class JiraPerfRequest:
    """Structured performance-test request derived from a Jira issue."""

    target_url: str = ""
    recording_hint: str = ""
    workload: Dict[str, Any] = field(default_factory=dict)
    thresholds: Dict[str, Any] = field(default_factory=dict)
    credential_env_refs: Dict[str, str] = field(default_factory=dict)
    raw_parse: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    goal: str = ""

    @property
    def host_hint(self) -> str:
        if self.recording_hint:
            return self.recording_hint
        try:
            return urlparse(self.target_url).netloc or ""
        except Exception:
            return ""


def _merge_workload(data: Dict[str, Any]) -> Dict[str, Any]:
    wl = dict(data.get("workload") or {})
    for key in ("vus", "duration", "stages", "iterations", "executor", "maxDuration"):
        if key in data and key not in wl:
            wl[key] = data[key]
    thr = data.get("thresholds") or data.get("sla") or {}
    if thr and "thresholds" not in wl:
        wl["thresholds"] = thr
    return wl


def _looks_like_nfe_config(data: Dict[str, Any]) -> bool:
    keys = {str(k).lower() for k in data.keys()}
    return bool(
        keys
        & {
            "target_url",
            "url",
            "recording",
            "recording_hint",
            "workload",
            "thresholds",
            "vus",
            "iterations",
            "credential_env",
            "credentials_env",
        }
    )


def _parse_structured_block(text: str) -> Optional[Dict[str, Any]]:
    for m in _FENCE_RE.finditer(text or ""):
        body = m.group(1).strip()
        try:
            if body.lstrip().startswith("{"):
                parsed = json.loads(body)
            else:
                parsed = yaml.safe_load(body)
            if isinstance(parsed, dict) and _looks_like_nfe_config(parsed):
                return parsed
        except Exception as exc:
            logger.debug("Structured fence parse failed: %s", exc)

    # Unfenced YAML after an NFE config heading
    for marker in (
        r"##\s*NFE\s*config",
        r"#\s*NFE\s*config",
        r"NFE\s*config\s*:?",
    ):
        m = re.search(marker, text or "", re.IGNORECASE)
        if not m:
            continue
        tail = (text or "")[m.end() :].strip()
        # Drop following markdown headings
        tail = re.split(r"\n#{1,6}\s+", tail, maxsplit=1)[0].strip()
        # Prefer fenced content already handled; try yaml load of remaining
        try:
            parsed = yaml.safe_load(tail)
            if isinstance(parsed, dict) and _looks_like_nfe_config(parsed):
                return parsed
        except Exception:
            pass

    stripped = (text or "").strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and _looks_like_nfe_config(parsed):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def _extract_goal(text: str) -> str:
    m = re.search(
        r"(?:^|\n)#+\s*Goal\s*\n+(.+?)(?=\n#+\s|\Z)",
        text or "",
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return " ".join(m.group(1).split())
    return ""


def parse_story_text(
    *,
    summary: str = "",
    description: str = "",
    acceptance_criteria: str = "",
    validate_url: bool = True,
) -> JiraPerfRequest:
    """Extract target URL, recording hint, and workload from story text.

    Prefers YAML/JSON fenced blocks (including ADF codeBlock → fences);
    falls back to first URL / prose cues. Credential values are never stored —
    only optional env ref names from the structured block.
    """
    combined = "\n\n".join(
        p for p in (summary, description, acceptance_criteria) if p
    )
    safe_for_log = redact_text_for_llm(combined)
    req = JiraPerfRequest()
    data = _parse_structured_block(combined) or {}
    req.raw_parse = dict(data)
    req.goal = _extract_goal(combined)

    url = data.get("target_url") or data.get("url") or ""
    if not url:
        m = _URL_RE.search(combined)
        if m:
            url = m.group(0).rstrip(".,)")

    req.target_url = str(url or "").strip()
    req.recording_hint = str(
        data.get("recording")
        or data.get("recording_hint")
        or data.get("host")
        or ""
    ).strip()
    if not req.recording_hint:
        rm = _RECORDING_RE.search(combined)
        if rm:
            req.recording_hint = rm.group(1).strip().strip(".,;")

    req.workload = _merge_workload(data)
    # Prose cue: "10 virtual users"
    if "vus" not in req.workload:
        vm = _VUS_RE.search(combined)
        if vm:
            try:
                req.workload["vus"] = int(vm.group(1))
            except ValueError:
                pass

    req.thresholds = dict(
        data.get("thresholds") or data.get("sla") or req.workload.get("thresholds") or {}
    )
    if req.thresholds and "thresholds" not in req.workload:
        req.workload["thresholds"] = dict(req.thresholds)

    creds = data.get("credential_env") or data.get("credentials_env") or {}
    if isinstance(creds, dict):
        req.credential_env_refs = {str(k): str(v) for k, v in creds.items()}

    if not req.target_url and not req.recording_hint:
        req.errors.append(
            "No target_url or recording hint found in description / acceptance criteria."
        )
    elif not req.target_url and req.recording_hint:
        # URL may be filled from the Watch-me recording later
        logger.info(
            "Story has recording=%r but no target_url; will try recording file",
            req.recording_hint,
        )
    elif req.target_url and validate_url:
        try:
            assert_url_allowed(req.target_url)
        except UrlPolicyError as exc:
            req.errors.append(f"target_url rejected by URL policy: {exc}")

    logger.info(
        "Parsed Jira perf request: url=%s recording=%s workload_keys=%s (%s chars story)",
        bool(req.target_url),
        req.recording_hint or "",
        list(req.workload.keys()),
        len(safe_for_log),
    )
    return req
