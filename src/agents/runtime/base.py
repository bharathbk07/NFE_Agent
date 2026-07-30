"""Sub-agent protocol for the PE assistant runtime."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Sequence

from src.agents.runtime.contracts import HandoffResult, SpecialistId


class SubAgent(ABC):
    """Specialist that accepts capability tags and returns a HandoffResult."""

    id: SpecialistId
    accepts_capabilities: Sequence[str] = ()

    @abstractmethod
    async def run(
        self,
        *,
        goal: str,
        question: str,
        context_pack: Dict[str, Any],
        need_tools: bool = True,
    ) -> HandoffResult:
        """Execute one specialist turn for ``goal``."""
