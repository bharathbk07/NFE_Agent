"""Registry of PE specialist sub-agents."""
from __future__ import annotations

from typing import Dict, List, Optional

from src.agents.runtime.base import SubAgent
from src.agents.runtime.contracts import SpecialistId

_REGISTRY: Dict[str, SubAgent] = {}


def register_sub_agent(agent: SubAgent) -> SubAgent:
    """Register (or replace) a specialist by id."""
    _REGISTRY[str(agent.id)] = agent
    return agent


def get_sub_agent(specialist_id: SpecialistId | str) -> Optional[SubAgent]:
    """Return a registered specialist, if any."""
    return _REGISTRY.get(str(specialist_id))


def list_sub_agents() -> List[SubAgent]:
    """Return all registered specialists."""
    return list(_REGISTRY.values())


def clear_registry() -> None:
    """Clear registrations (tests only)."""
    _REGISTRY.clear()


def ensure_default_agents() -> None:
    """Lazily register the built-in specialist set."""
    if _REGISTRY:
        return
    from src.agents.specialists.evidence_trends import EvidenceTrendsAgent
    from src.agents.specialists.integrations import IntegrationsAgent
    from src.agents.specialists.knowledge_qa import KnowledgeQAAgent
    from src.agents.specialists.scripting import ScriptingAdviseAgent

    register_sub_agent(KnowledgeQAAgent())
    register_sub_agent(IntegrationsAgent())
    register_sub_agent(EvidenceTrendsAgent())
    register_sub_agent(ScriptingAdviseAgent())
