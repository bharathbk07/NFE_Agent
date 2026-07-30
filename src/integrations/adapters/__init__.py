"""Pluggable evidence / integration adapters for PE assistant."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class EvidenceBundle:
    """Normalized evidence returned by an EvidenceSource."""

    markdown: str = ""
    citations: List[Dict[str, str]] = field(default_factory=list)
    source_id: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


class EvidenceSource(ABC):
    """Sync external evidence into local knowledge/RAG (write-through)."""

    id: str = "base"

    @abstractmethod
    def sync(
        self,
        question: str,
        *,
        app: str = "",
        flow: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> EvidenceBundle:
        """Fetch/sync evidence for ``question`` scoped to app/flow."""


class IntegrationAdapter(ABC):
    """ALM-style list/get/search adapter (REST or other transports)."""

    id: str = "base"
    capabilities: List[str] = []

    @abstractmethod
    def list_items(self, **kwargs: Any) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    def get_item(self, key: str, **kwargs: Any) -> Dict[str, Any]:
        ...

    def search(self, query: str, **kwargs: Any) -> List[Dict[str, Any]]:
        return []


_EVIDENCE: Dict[str, EvidenceSource] = {}
_INTEGRATIONS: Dict[str, IntegrationAdapter] = {}


def register_evidence_source(source: EvidenceSource) -> EvidenceSource:
    _EVIDENCE[source.id] = source
    return source


def register_integration_adapter(adapter: IntegrationAdapter) -> IntegrationAdapter:
    _INTEGRATIONS[adapter.id] = adapter
    return adapter


def get_evidence_source(source_id: str) -> Optional[EvidenceSource]:
    return _EVIDENCE.get(source_id)


def list_evidence_sources() -> List[EvidenceSource]:
    return list(_EVIDENCE.values())


def get_integration_adapter(adapter_id: str) -> Optional[IntegrationAdapter]:
    return _INTEGRATIONS.get(adapter_id)


def list_integration_adapters() -> List[IntegrationAdapter]:
    return list(_INTEGRATIONS.values())


def ensure_default_adapters() -> None:
    if _EVIDENCE and _INTEGRATIONS:
        return
    from src.integrations.adapters.confluence_evidence import ConfluenceEvidenceAdapter
    from src.integrations.adapters.jira_rest import JiraRestAdapter
    from src.integrations.adapters.monitoring_stub import MonitoringStubAdapter

    register_evidence_source(ConfluenceEvidenceAdapter())
    register_evidence_source(MonitoringStubAdapter())
    register_integration_adapter(JiraRestAdapter())
