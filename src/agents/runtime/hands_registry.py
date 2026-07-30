"""Hands registry — PE-scoped tools with OpenClaw-style risk tiers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field


class RiskTier(str, Enum):
    READ = "read"
    MUTATE = "mutate"
    EXECUTE = "execute"


@dataclass
class HandSpec:
    """Metadata for one PE Hand (tool)."""

    name: str
    description: str
    risk: RiskTier
    capability: str  # jira | confluence | playwright | knowledge | trends | scripting | report
    tool: Any
    requires_confirm_unless_authorized: bool = False
    auth_keys: List[str] = field(default_factory=list)
    # e.g. auth_keys=["execute_story"] means prior thread auth for execute_story skips confirm


class HandsRegistry:
    """Collect PE Hands and expose LangChain tools for the Brain."""

    def __init__(self) -> None:
        self._hands: Dict[str, HandSpec] = {}

    def register(self, spec: HandSpec) -> None:
        self._hands[spec.name] = spec

    def get(self, name: str) -> Optional[HandSpec]:
        return self._hands.get(name)

    def list_specs(self) -> List[HandSpec]:
        return list(self._hands.values())

    def tools(
        self,
        *,
        include_execute: bool = True,
        include_mutate: bool = True,
    ) -> List[Any]:
        out: List[Any] = []
        for spec in self._hands.values():
            if spec.risk == RiskTier.EXECUTE and not include_execute:
                continue
            if spec.risk == RiskTier.MUTATE and not include_mutate:
                continue
            out.append(spec.tool)
        return out

    def catalog_text(self) -> str:
        lines = []
        for spec in sorted(self._hands.values(), key=lambda s: (s.capability, s.name)):
            lines.append(
                f"- `{spec.name}` [{spec.risk.value}/{spec.capability}] — {spec.description}"
            )
        return "\n".join(lines) or "(no hands registered)"


def make_tool(
    *,
    name: str,
    description: str,
    func: Callable[..., str],
    args_schema: type[BaseModel],
) -> StructuredTool:
    """Build a StructuredTool with async coroutine wrapper."""

    async def _coro(**kwargs: Any) -> str:
        return func(**kwargs)

    tool = StructuredTool.from_function(
        name=name,
        description=description,
        func=func,
        args_schema=args_schema,
    )
    tool.coroutine = _coro  # type: ignore[attr-defined]
    return tool


# --- Common arg schemas ---


class EmptyArgs(BaseModel):
    pass


class IssueKeyArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key, e.g. SCRUM-1")


class QueryArgs(BaseModel):
    query: str = Field(description="Search or question text")


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(description="Search or question text")
    app: str = Field(default="", description="Optional app scope")
    flow: str = Field(default="", description="Optional flow scope")


class JiraCommentArgs(BaseModel):
    issue_key: str = Field(description="Jira issue key")
    body: str = Field(description="Comment markdown/text")


class SmokeArgs(BaseModel):
    script_path: str = Field(
        default="",
        description="Path to k6 script; empty uses session artifact if present",
    )


class AppFlowArgs(BaseModel):
    app: str = Field(default="", description="App id / host")
    flow: str = Field(default="", description="Flow id e.g. create-claim")


class TrendsArgs(BaseModel):
    question: str = Field(default="trend report")
    app: str = Field(default="")
    flow: str = Field(default="")
    exclude_smoke: bool = Field(default=False)
    min_vus: int = Field(default=0)


class CreateIssueArgs(BaseModel):
    summary: str = Field(description="Issue summary")
    description: str = Field(description="Detailed description / RCA")
    acceptance_criteria: str = Field(default="", description="AC checklist text")
    parent_key: str = Field(default="", description="Optional parent story key to link")
    labels: str = Field(default="nfe-analysis", description="Comma-separated labels")


class ExecuteStoryArgs(BaseModel):
    issue_key: str = Field(description="Story key to execute")
    force: bool = Field(default=False, description="Force re-run if In Progress")


class WatchMeArgs(BaseModel):
    target_url: str = Field(description="URL to open for Watch-me recording")
    label: str = Field(default="", description="Optional flow label")


class ReuseRecordingArgs(BaseModel):
    app: str = Field(default="", description="App id")
    flow: str = Field(default="default", description="Recording / flow stem")


class LoadSkillArgs(BaseModel):
    skill_id: str = Field(description="Skill id from the catalog, e.g. run-story-with-analysis")


class RankStoriesArgs(BaseModel):
    user_goal: str = Field(description="User goal used to rank stories")
