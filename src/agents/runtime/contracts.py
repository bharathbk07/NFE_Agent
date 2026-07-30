"""Contract-first schemas for PE supervisor ↔ specialist handoffs."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

CitationSource = Literal["session", "knowledge", "rag", "tool", "mcp", "adapter"]

SpecialistId = Literal[
    "knowledge_qa",
    "evidence_trends",
    "integrations",
    "scripting",
]


class Citation(BaseModel):
    """One grounded evidence pointer for a specialist answer."""

    source: CitationSource = Field(description="Evidence layer that supplied the fact")
    ref: str = Field(default="", description="Path, issue key, tool name, or label")
    snippet: str = Field(default="", description="Short supporting excerpt")


class PlanStep(BaseModel):
    """One specialist delegation in a supervisor plan."""

    specialist: SpecialistId
    goal: str = Field(description="What this specialist must answer or fetch")
    need_tools: bool = Field(
        default=True,
        description="Whether the specialist should call tools for this goal",
    )


class SupervisorPlan(BaseModel):
    """Structured plan produced by the PE supervisor."""

    steps: List[PlanStep] = Field(default_factory=list, max_length=4)
    synthesize: bool = Field(
        default=True,
        description="Whether to run a final synthesize pass over specialist results",
    )
    direct_answer: Optional[str] = Field(
        default=None,
        description="If set, supervisor answers without delegation (context already enough)",
    )
    reason: str = Field(default="", description="Brief planning rationale")


class HandoffResult(BaseModel):
    """Structured result returned by a specialist sub-agent."""

    specialist: SpecialistId
    answer_md: str = Field(default="", description="Markdown answer fragment")
    citations: List[Citation] = Field(default_factory=list)
    tool_calls: List[str] = Field(
        default_factory=list,
        description="Tool names invoked this turn",
    )
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    missing: List[str] = Field(
        default_factory=list,
        description="What could not be found or needs a pipeline run",
    )
