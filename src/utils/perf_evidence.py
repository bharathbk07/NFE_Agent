"""Cache-first perf evidence: local knowledge/RAG first; tools sync on miss/refresh."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Protocol

from src.utils.knowledge_store import ingest_run_history, list_run_history
from src.utils.perf_trend import (
    build_trend_table,
    filter_kpi_rows,
    parse_trend_filters,
    wants_confluence_evidence,
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
        try_sibling_flows: bool = True,
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
        flow_candidates = _flow_title_candidates(flow, target_url, resolve_flow_name, sanitize_title)

        root = client.find_page_by_title(parent_title, parent_id=None)
        if not root:
            logger.info("Confluence sync: parent page not found (%s)", parent_title)
            return []
        root_id = str(root.get("id") or "")

        flow_page = None
        used_flow = flow or "default"
        for candidate in flow_candidates:
            flow_page = client.find_page_by_title(candidate, parent_id=root_id)
            if not flow_page:
                flow_page = client.find_page_by_title(candidate, parent_id=None)
            if flow_page:
                used_flow = candidate
                break

        if not flow_page and try_sibling_flows:
            # Scan child pages of parent for any flow that has Run children
            try:
                siblings = client.list_child_pages(root_id, limit=30)
            except Exception:
                siblings = []
            for sib in siblings:
                title = str(sib.get("title") or "")
                if title.lower().startswith("run "):
                    continue
                kids = client.list_child_pages(str(sib.get("id") or ""), limit=5)
                if any(str(k.get("title") or "").lower().startswith("run ") for k in kids):
                    flow_page = sib
                    used_flow = title
                    break

        if not flow_page:
            logger.info(
                "Confluence sync: flow page not found (tried %s)",
                flow_candidates,
            )
            return []

        flow_page_id = str(flow_page.get("id") or "")
        children = client.list_child_pages(flow_page_id, limit=20)
        run_pages = [
            p
            for p in children
            if str(p.get("title") or "").lower().startswith("run ")
        ][:12]

        from src.utils.app_registry import slug_flow

        flow_slug = slug_flow(flow) if flow else ""
        used_slug = slug_flow(used_flow) if used_flow else ""
        # Prefer explicit non-default flow (e.g. create-claim) so local history
        # stays aligned with the story even when Confluence parent is "default".
        if flow_slug and flow_slug != "default":
            ingest_flow = flow_slug
        else:
            ingest_flow = used_slug or flow_slug or "default"

        if not (app or "").strip():
            logger.warning(
                "Confluence sync: app id empty — cannot ingest %s Run page(s)",
                len(run_pages),
            )
            return []

        ingested: List[Dict[str, Any]] = []
        for page in run_pages:
            page_id = str(page.get("id") or "")
            title = str(page.get("title") or "")
            kpis: Dict[str, Any] = {
                "run_id": f"confluence_{page_id}",
                "timestamp": title.replace("Run ", "").strip(),
                "source": "confluence_sync",
                "confluence_url": client.page_url(page_id) if page_id else "",
                "workload_source": "confluence_run",
            }
            body = (
                ((page.get("body") or {}).get("storage") or {}).get("value") or ""
            )
            # Re-fetch with body if list_child omitted storage
            if not body and page_id:
                try:
                    full = client.get_page(page_id, expand="body.storage,version")
                    body = (
                        ((full.get("body") or {}).get("storage") or {}).get("value")
                        or ""
                    )
                except Exception as exc:
                    logger.debug("get_page body failed for %s: %s", page_id, exc)
            body = redact_text_for_llm(str(body))
            kpis.update(_kpis_from_confluence_storage(body))
            try:
                path = ingest_run_history(
                    app,
                    ingest_flow,
                    kpis=kpis,
                    target_url=target_url,
                    confluence_url=str(kpis.get("confluence_url") or ""),
                    workload_source=str(kpis.get("workload_source") or "confluence_run"),
                )
                kpis["path"] = str(path)
                kpis["flow_synced"] = used_flow
            except Exception as exc:
                logger.warning("Ingest from Confluence page failed: %s", exc)
                continue
            ingested.append(kpis)
        return ingested


def _flow_title_candidates(
    flow: str,
    target_url: str,
    resolve_flow_name,
    sanitize_title,
) -> List[str]:
    names: List[str] = []
    if flow:
        names.append(sanitize_title(flow))
    resolved = resolve_flow_name(
        recording_file="",
        recording_hint=flow or "",
        target_url=target_url or "",
    )
    if resolved:
        names.append(sanitize_title(resolved))
    for extra in ("default", "create-claim", "Create Claim"):
        names.append(sanitize_title(extra))
    # de-dupe preserve order
    seen = set()
    out: List[str] = []
    for n in names:
        key = (n or "").lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _kpis_from_confluence_storage(body: str) -> Dict[str, Any]:
    """Best-effort extract of p95 / fail rate / VUs from NFE storage HTML."""
    out: Dict[str, Any] = {}
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
        r"(?:HTTP )?error rate</strong></p><p>([^<]+)</p>",
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
    # VUs (plan/max) cell: "10 / 10" or "10 / n/a"
    m = re.search(
        r"VUs?\s*\(plan/?max\)</strong></p><p>([^<]+)</p>",
        body or "",
        re.I,
    )
    if not m:
        m = re.search(r"\b(\d+)\s*/\s*(\d+|n/?a)\b", body or "", re.I)
    if m:
        try:
            first = re.match(r"\s*(\d+)", m.group(1) or "")
            planned = int(first.group(1)) if first else 0
            if planned > 0:
                out["vus"] = planned
        except (ValueError, IndexError):
            pass
    if re.search(r"\b(jira_story|story workload)\b", body or "", re.I):
        out["workload_source"] = "jira_story"
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


def sync_confluence_and_build_report(
    *,
    app: str,
    flow: str,
    question: str = "",
    target_url: str = "",
    force: bool = True,
    exclude_smoke: Optional[bool] = None,
    min_vus: Optional[int] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Sync Confluence Run pages, filter KPIs, and return a trend report dict."""
    from src.utils.app_registry import resolve_evidence_scope

    filters = parse_trend_filters(question)
    if exclude_smoke is None:
        exclude_smoke = bool(filters.get("exclude_smoke"))
    if min_vus is None:
        min_vus = int(filters.get("min_vus") or 0)

    app, flow, target_url = resolve_evidence_scope(
        question=question,
        app=app,
        flow=flow,
        target_url=target_url,
        state=state,
    )

    notes: List[str] = []
    sources: List[str] = []
    if not app:
        notes.append(
            "Could not resolve an app id (no session URL, NFE_DEFAULT_APP, or local knowledge). "
            "Confluence Run pages cannot be ingested until scope is known."
        )
    else:
        notes.append(f"Resolved evidence scope: app=`{app}` flow=`{flow or 'default'}`.")

    conf = ConfluenceEvidenceSource()
    remote: List[Dict[str, Any]] = []
    try:
        remote = conf.sync(
            app,
            flow or "default",
            force=force,
            target_url=target_url,
            try_sibling_flows=True,
        )
    except Exception as exc:
        err = str(exc)
        if "403" in err or "401" in err or "Forbidden" in err or "Unauthorized" in err:
            notes.append(
                f"Confluence REST auth failed ({err}). "
                "Local knowledge still used for the trend table. "
                "Fix CONFLUENCE_EMAIL / CONFLUENCE_API_TOKEN (needs Confluence read) "
                "or republish runs so pages are readable by this token."
            )
        else:
            notes.append(f"Confluence sync failed: {exc}")
        logger.warning("sync_confluence_and_build_report: %s", exc)

    if remote:
        sources.append("confluence_sync")
        notes.append(
            f"Synced {len(remote)} Confluence Run page(s) into local knowledge / RAG."
        )
        flow_used = str(remote[0].get("flow_synced") or flow or "default")
        if flow_used and flow and flow_used.lower() != (flow or "").lower():
            notes.append(f"Used Confluence flow page `{flow_used}` (requested `{flow}`).")
    else:
        notes.append(
            f"Confluence sync returned 0 Run pages for app=`{app or '(none)'}` "
            f"flow=`{flow or 'default'}`. "
            "Check CONFLUENCE_* credentials and that Run child pages exist under the flow page."
        )

    # Prefer ingest_flow from sync when sibling used
    pack_flow = flow or "default"
    if remote and remote[0].get("flow_synced"):
        # Also load history under requested flow
        kpis_a = list_run_history(app, pack_flow, limit=15)
        kpis_b = list_run_history(app, str(remote[0]["flow_synced"]), limit=15)
        # Merge by run_id
        by_id = {str(k.get("run_id")): k for k in kpis_a + kpis_b if k.get("run_id")}
        kpis = list(by_id.values())
    else:
        kpis, _ = local_trend_pack(app, pack_flow, limit=15)
        # Sibling local flows when requested flow is empty of history
        if not kpis and pack_flow != "default":
            kpis, _ = local_trend_pack(app, "default", limit=15)
        elif not kpis and pack_flow == "default":
            kpis_alt, _ = local_trend_pack(app, "create-claim", limit=15)
            if kpis_alt:
                kpis = kpis_alt
                notes.append("Loaded local runs from flow `create-claim` (default had none).")

    # Prefer confluence_sync rows when present
    conf_rows = [k for k in kpis if str(k.get("source") or "") == "confluence_sync"]
    if conf_rows:
        kpis = conf_rows + [
            k for k in kpis if str(k.get("source") or "") != "confluence_sync"
        ]

    filtered, filter_notes = filter_kpi_rows(
        kpis, exclude_smoke=bool(exclude_smoke), min_vus=int(min_vus or 0)
    )
    notes.extend(filter_notes)
    table = build_trend_table(filtered, limit=10)

    header = f"### Trend report from Confluence sync ({len(remote)} page(s) synced)\n\n"
    if not remote and filtered:
        header = "### Trend report (local knowledge; Confluence sync empty)\n\n"
    elif not remote and not filtered:
        header = "### Trend report unavailable\n\n"

    md_parts = [header, table]
    if notes:
        md_parts.append("\n\n**Notes:**\n" + "\n".join(f"- {n}" for n in notes))
    urls = [
        str(k.get("confluence_url"))
        for k in filtered
        if k.get("confluence_url") and str(k.get("confluence_url")).startswith("http")
    ][:5]
    if urls:
        md_parts.append(
            "\n\n**Confluence run pages:**\n"
            + "\n".join(f"- {u}" for u in urls)
        )

    return {
        "sources": sources or (["knowledge_markdown"] if filtered else []),
        "kpis": filtered,
        "synced_count": len(remote),
        "trend_markdown": "".join(md_parts),
        "notes": notes,
        "exclude_smoke": exclude_smoke,
        "min_vus": min_vus,
        "app": app,
        "flow": flow or "default",
        "target_url": target_url,
    }


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
    need_refresh = (
        force_refresh
        or wants_tool_refresh(question)
        or wants_confluence_evidence(question)
    )
    if need_refresh or (
        wants_trend_question(question)
        and len(list_run_history(app, flow, limit=5)) < 2
    ):
        report = sync_confluence_and_build_report(
            app=app,
            flow=flow,
            question=question,
            target_url=target_url,
            force=True,
        )
        if report.get("synced_count") or report.get("kpis"):
            return report

    notes: List[str] = []
    sources: List[str] = []
    kpis, table = local_trend_pack(app, flow)
    if kpis:
        sources.append("knowledge_markdown")

    filters = parse_trend_filters(question)
    filtered, filter_notes = filter_kpi_rows(
        kpis,
        exclude_smoke=bool(filters.get("exclude_smoke")),
        min_vus=int(filters.get("min_vus") or 0),
    )
    notes.extend(filter_notes)
    table = build_trend_table(filtered, limit=10)

    if "monitoring" in (question or "").lower() or "grafana" in (question or "").lower():
        MonitoringEvidenceStub().sync(app, flow, force=True, target_url=target_url)
        notes.append(
            "Monitoring tool sync is not configured yet; using local knowledge/RAG only."
        )
        sources.append("monitoring_stub")

    if not filtered and wants_trend_question(question):
        notes.append(
            "No run history yet. Publish runs to Confluence or ask to retrieve Confluence data."
        )

    return {
        "sources": sources,
        "kpis": filtered,
        "trend_markdown": table,
        "notes": notes,
    }
