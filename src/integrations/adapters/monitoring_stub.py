"""Monitoring evidence stub — replace with Grafana/Datadog adapters later."""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.integrations.adapters import EvidenceBundle, EvidenceSource


class MonitoringStubAdapter(EvidenceSource):
    id = "monitoring"

    def sync(
        self,
        question: str,
        *,
        app: str = "",
        flow: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> EvidenceBundle:
        return EvidenceBundle(
            markdown=(
                "Monitoring adapters are not configured yet. "
                "Local smoke/KPI evidence remains the source of truth."
            ),
            source_id=self.id,
            citations=[{"source": "adapter", "ref": "monitoring_stub"}],
            meta={"implemented": False},
        )
