"""PE Skills — OpenClaw-style Markdown playbooks loaded on demand."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_SKILLS_ROOT = Path(__file__).resolve().parents[3] / "skills" / "pe"


@dataclass
class SkillMeta:
    id: str
    name: str
    description: str
    path: Path

    def catalog_line(self) -> str:
        return f"- `{self.id}` — {self.description}"


def skills_root() -> Path:
    return _SKILLS_ROOT


def list_skills() -> List[SkillMeta]:
    root = skills_root()
    if not root.is_dir():
        return []
    out: List[SkillMeta] = []
    for path in sorted(root.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        sid = path.stem
        name = sid
        desc = ""
        m = re.search(r"^#\s+(.+)$", text, re.M)
        if m:
            name = m.group(1).strip()
        m = re.search(r"^>\s*(.+)$", text, re.M)
        if m:
            desc = m.group(1).strip()
        else:
            # first non-empty non-heading line
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and not s.startswith(">"):
                    desc = s[:160]
                    break
        out.append(SkillMeta(id=sid, name=name, description=desc or name, path=path))
    return out


def catalog_text() -> str:
    skills = list_skills()
    if not skills:
        return "(no PE skills installed yet)"
    return "\n".join(s.catalog_line() for s in skills)


def load_skill(skill_id: str) -> str:
    """Return full skill markdown or an error string."""
    sid = (skill_id or "").strip().replace(".md", "")
    if not sid or "/" in sid or "\\" in sid or ".." in sid:
        return f"Invalid skill id: {skill_id!r}"
    path = skills_root() / f"{sid}.md"
    if not path.is_file():
        known = ", ".join(s.id for s in list_skills()) or "(none)"
        return f"Unknown skill `{sid}`. Known: {known}"
    try:
        return path.read_text(encoding="utf-8")[:12000]
    except Exception as exc:
        logger.warning("load_skill failed: %s", exc)
        return f"Failed to load skill: {exc}"


def skill_index() -> Dict[str, SkillMeta]:
    return {s.id: s for s in list_skills()}
