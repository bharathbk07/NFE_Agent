"""Exec Approval — OpenClaw-style policy for risky Hands."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set

from src.agents.runtime.hands_registry import HandSpec, RiskTier


_AUTH_PATTERNS: List[tuple[str, re.Pattern[str]]] = [
    (
        "create_analysis_issue",
        re.compile(
            r"\b(creat(e|ed)|open|rais(e|ed)|fil(e|ed))\b.{0,50}\b("
            r"analys|analysis|ticket|issue|jira"
            r")\b|"
            r"\bif\s+.+\s+fail.+\s+creat",
            re.I,
        ),
    ),
    (
        "execute_story",
        re.compile(
            r"\b(work\s+on|run|execute|process)\b.{0,40}\b("
            r"stor(y|ies)|jira|scrum|performance\s+test"
            r")\b|"
            r"\bperformance\s+user\s+stor",
            re.I,
        ),
    ),
    (
        "watch_me",
        re.compile(
            r"\b(watch\s*me|record(\s+me)?|capture\s+(the\s+)?(flow|journey))\b",
            re.I,
        ),
    ),
    (
        "publish_confluence",
        re.compile(
            r"\b(publish|sync|post).{0,30}\bconfluence\b|"
            r"\bconfluence\b.{0,30}\b(publish|sync|update)\b",
            re.I,
        ),
    ),
]


@dataclass
class PendingAction:
    kind: str
    hand_name: str
    args: Dict[str, Any] = field(default_factory=dict)
    ask: str = ""
    auth_keys: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "hand_name": self.hand_name,
            "args": self.args,
            "ask": self.ask,
            "auth_keys": self.auth_keys,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["PendingAction"]:
        if not data or not isinstance(data, dict):
            return None
        return cls(
            kind=str(data.get("kind") or ""),
            hand_name=str(data.get("hand_name") or ""),
            args=dict(data.get("args") or {}),
            ask=str(data.get("ask") or ""),
            auth_keys=list(data.get("auth_keys") or []),
        )


def infer_authorizations(text: str) -> Set[str]:
    found: Set[str] = set()
    for key, pat in _AUTH_PATTERNS:
        if pat.search(text or ""):
            found.add(key)
    return found


def merge_authorizations(
    existing: Optional[List[str]], new: Set[str]
) -> List[str]:
    out = set(existing or [])
    out |= new
    return sorted(out)


_CONFIRM_YES = re.compile(
    r"^\s*(yes|y|ok|okay|proceed|go\s+ahead|confirm|do\s+it|sure)\s*[.!?]?\s*$",
    re.I,
)
_CONFIRM_NO = re.compile(
    r"^\s*(no|n|cancel|stop|don'?t|do\s+not)\s*[.!?]?\s*$",
    re.I,
)


def is_confirm_yes(text: str) -> bool:
    return bool(_CONFIRM_YES.match((text or "").strip()))


def is_confirm_no(text: str) -> bool:
    return bool(_CONFIRM_NO.match((text or "").strip()))


def needs_confirmation(
    spec: HandSpec,
    *,
    authorizations: Sequence[str] | Set[str],
) -> bool:
    if spec.risk == RiskTier.READ:
        return False
    auth = set(authorizations or [])
    if not spec.requires_confirm_unless_authorized:
        if spec.risk == RiskTier.EXECUTE:
            return not any(k in auth for k in (spec.auth_keys or ["execute_story"]))
        return False
    keys = spec.auth_keys or [spec.name]
    return not any(k in auth for k in keys)
