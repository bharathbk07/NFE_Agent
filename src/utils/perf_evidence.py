"""Cache-first perf evidence: local knowledge/RAG first; tools sync on miss/refresh."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Protocol

from src.utils.knowledge_store import ingest_run_history, list_run_history
from src.utils.perf_trend import (
    build_trend_table,
    wants_tool_refresh,
    wants_trend_question,
)

logger = logging.getLogger(__name__)


class PerfEvidenceSource(Protocol):
    """Remote evidence sync that writes into knowledge + RAG."""

    name: str

    def sync(
        self,
        app: str,
        flow: str,
        *,
        force: bool = False,
        target_url: str = "",
    ) -> List[Dict[str, Any]]:
        ...


class MonitoringEvidenceStub:
    """Placeholder for future Grafana/Datadog/etc. integrations."""

    name = "monitoring"

    def sync(
        self,
        app: str,
        flow: str,
        *,
        force: bool = False,
        target_url: str = "",
    ) -> List[Dict[str, Any]]:
        logger.info(
            "Monitoring evidence source not configured (app=%s flow=%s force=%s)",
            app,
            flow,
            force,
        )
        return []


class ConfluenceEvidenceSource:
    """Pull Run child pages under the NFE flow hierarchy; ingest locally."""

    name = "confluence"

    def sync(
        self,
        app: str,
        flow: str,
        *,
        force: bool = False,
        target_url: str = "",
    ) -> List[Dict[str, Any]]:
        _ = force
        try:
            from config.settings import settings
            from src.integrations.confluence.client import ConfluenceClient
            from src.integrations.confluence.publisher import (
                _has_confluence_credentials,
                resolve_flow_name,
            )
            from src.integrations.confluence.security import sanitize_title
            from src.security.secrets import redact_text_for_llm
        except Exception as exc:
            logger.warning("Confluence sync imports failed: %s", exc)
            return []

        if not _has_confluence_credentials():
            logger.info("Confluence sync skipped: missing credentials")
            return []

        try:
            client = ConfluenceClient()
        except Exception as exc:
            logger.warning("Confluence client init failed: %s", exc)
            return []

        parent_title = sanitize_title(
            settings.CONFLUENCE_PARENT_TITLE or "Performance Testing and Engineering"
        )
        flow_name = resolve_flow_name(
            recording_file="",
            recording_hint=flow or "",
            target_url=target_url or "",
        )
        # Prefer app/flow slug as flow page title when resolve uses host only
        if flow and flow not in ("default",):
            flow_name = sanitize_title(flow)

        root = client.find_page_by_title(parent_title, parent_id=None)
        if not root:
            logger.info("Confluence sync: parent page not found (%s)", parent_title)
            return []
        root_id = str(root.get("id") or "")
        flow_page = client.find_page_by_title(flow_name, parent_id=root_id)
        if not flow_page:
            # try without parent filter
            flow_page = client.find_page_by_title(flow_name, parent_id=None)
        if not flow_page:
            logger.info("Confluence sync: flow page not found (%s)", flow_name)
            return []

        flow_page_id = str(flow_page.get("id") or "")
        children = client.list_child_pages(flow_page_id, limit=15)
        run_pages = [
            p
            for p in children
            if str(p.get("title") or "").lower().startswith("run ")
        ][:10]

        ingested: List[Dict[str, Any]] = []
        for page in run_pages:
            page_id = str(page.get("id") or "")
            title = str(page.get("title") or "")
            kpis: Dict[str, Any] = {
                "run_id": f"confluence_{page_id}",
                "timestamp": title.replace("Run ", "").strip(),
                "source": "confluence_sync",
                "confluence_url": client.page_url(page_id) if page_id else "",
            }
            # Prefer KPI markers from storage body
            body = (
                ((page.get("body") or {}).get("storage") or {}).get("value") or ""
            )
            body = redact_text_for_llm(str(body))
            kpis.update(_kpis_from_confluence_storage(body))
            try:
                path = ingest_run_history(
                    app,
                    flow or "default",
                    kpis=kpis,
                    target_url=target_url,
                    confluence_url=str(kpis.get("confluence_url") or ""),
                )
                kpis["path"] = str(path)
            except Exception as exc:
                logger.warning("Ingest from Confluence page failed: %s", exc)
                continue
            ingested.append(kpis)
        return ingested


def _kpis_from_confluence_storage(body: str) -> Dict[str, Any]:
    """Best-effort extract of p95 / fail rate from NFE storage HTML."""
    out: Dict[str, Any] = {}
    # p95 latency cell text e.g. 123.4 ms or 1.2 s
    m = re.search(
        r"p95 latency</strong></p><p>([^<]+)</p>",
        body or "",
        re.I,
    )
    if m:
        raw = m.group(1).strip().lower()
        try:
            if raw.endswith("s") and "ms" not in raw:
                out["p95_ms"] = float(raw.replace("s", "").strip()) * 1000
            else:
                out["p95_ms"] = float(re.sub(r"[^0-9.]", "", raw) or "0")
        except ValueError:
            pass
    m = re.search(
        r"HTTP error rate</strong></p><p>([^<]+)</p>",
        body or "",
        re.I,
    )
    if not m:
        m = re.search(r">([0-9.]+)\s*%<", body or "")
    if m:
        raw = m.group(1).strip().replace("%", "")
        try:
            pct = float(re.sub(r"[^0-9.]", "", raw) or "0")
            out["fail_rate"] = pct / 100.0 if pct > 1 else pct
        except ValueError:
            pass
    if re.search(r"PASSED|COMPLETED", body or "", re.I):
        out.setdefault("smoke_ok", True)
    if re.search(r"FAILED|WATCHER STOPPED", body or "", re.I):
        out["smoke_ok"] = False
    return out


def local_trend_pack(
    app: str,
    flow: str,
    *,
    limit: int = 10,
) -> tuple[List[Dict[str, Any]], str]:
    """Load local run history and render a trend table."""
    kpis = list_run_history(app, flow, limit=limit)
    return kpis, build_trend_table(kpis, limit=limit)


def gather_evidence_for_question(
    question: str,
    *,
    app: str,
    flow: str,
    target_url: str = "",
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Retrieval ladder: local history → optional tool sync → trend table.

    Returns dict with keys: sources, kpis, trend_markdown, notes.
    """
    notes: List[str] = []
    sources: List[str] = []
    kpis, table = local_trend_pack(app, flow)
    if kpis:
        sources.append("knowledge_markdown")

    need_trend = wants_trend_question(question)
    need_refresh = force_refresh or wants_tool_refresh(question)
    local_miss = need_trend and len(kpis) < 2

    if need_refresh or local_miss:
        # Confluence first when asked or when local history too thin
        conf = ConfluenceEvidenceSource()
        try:
            remote = conf.sync(
                app, flow, force=need_refresh, target_url=target_url
            )
            if remote:
                sources.append("confluence_sync")
                notes.append(
                    f"Refreshed {len(remote)} run(s) from Confluence into local knowledge/RAG."
                )
                kpis, table = local_trend_pack(app, flow)
            elif need_refresh and "confluence" in (question or "").lower():
                notes.append(
                    "Confluence refresh returned no Run pages "
                    "(check credentials, parent/flow titles, or publish at least one run)."
                )
        except Exception as exc:
            logger.warning("Confluence evidence sync failed: %s", exc)
            notes.append(f"Confluence sync failed: {exc}")

        if "monitoring" in (question or "").lower() or "grafana" in (
            question or ""
        ).lower():
            stub = MonitoringEvidenceStub()
            stub.sync(app, flow, force=True, target_url=target_url)
            notes.append(
                "Monitoring tool sync is not configured yet; "
                "using local knowledge/RAG only."
            )
            sources.append("monitoring_stub")

    if not kpis and need_trend:
        notes.append(
            "No local run history yet. Complete a smoke/load run "
            "(or refresh from Confluence) so trends can be built from knowledge/RAG."
        )

    return {
        "sources": sources,
        "kpis": kpis,
        "trend_markdown": table,
        "notes": notes,
    }
