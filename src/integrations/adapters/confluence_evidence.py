"""Confluence evidence adapter (write-through via existing ladder helpers)."""
from __future__ import annotations

from typing import Any, Dict, Optional

from src.integrations.adapters import EvidenceBundle, EvidenceSource


class ConfluenceEvidenceAdapter(EvidenceSource):
    id = "confluence"

    def sync(
        self,
        question: str,
        *,
        app: str = "",
        flow: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> EvidenceBundle:
        state = state or {}
        target_url = str(state.get("target_url") or "")
        try:
            from src.utils.perf_evidence import sync_confluence_and_build_report

            report = sync_confluence_and_build_report(
                app=app or str(state.get("app") or ""),
                flow=flow or str(state.get("flow") or "default"),
                question=question,
                target_url=target_url,
                force=True,
            )
            citations = [
                {"source": "adapter", "ref": "confluence_sync"}
            ]
            for k in (report.get("kpis") or [])[:5]:
                url = k.get("confluence_url")
                if url:
                    citations.append({"source": "tool", "ref": str(url)})
            return EvidenceBundle(
                markdown=str(report.get("trend_markdown") or "")[:8000],
                citations=citations,
                source_id=self.id,
                meta=report,
            )
        except Exception as exc:
            return EvidenceBundle(
                markdown="",
                source_id=self.id,
                meta={"error": str(exc)},
            )
