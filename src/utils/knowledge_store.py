"""Markdown knowledge cards per application / flow."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.security.fs_jail import assert_under_jail
from src.utils.app_registry import artifacts_root, ensure_app_dirs, slug_flow

logger = logging.getLogger(__name__)


def knowledge_dir(app_id: str) -> Path:
    """Return ``artifacts/knowledge/<app>/`` (created lazily)."""
    ensure_app_dirs(app_id)
    return artifacts_root() / "knowledge" / app_id


def _jail_knowledge(path: Path) -> Path:
    root = artifacts_root() / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    return assert_under_jail(path, root)


def read_overview(app_id: str) -> str:
    """Return overview markdown for an app (empty string if missing)."""
    if not app_id:
        return ""
    path = knowledge_dir(app_id) / "overview.md"
    try:
        path = _jail_knowledge(path)
    except Exception:
        return ""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def read_flow(app_id: str, flow_id: str) -> str:
    """Return flow-card markdown (empty string if missing)."""
    flow = slug_flow(flow_id) or flow_id
    if not app_id or not flow:
        return ""
    path = knowledge_dir(app_id) / "flows" / f"{flow}.md"
    try:
        path = _jail_knowledge(path)
    except Exception:
        return ""
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def list_flows(app_id: str) -> List[str]:
    """List flow ids that have knowledge cards under an app."""
    if not app_id:
        return []
    flows_dir = knowledge_dir(app_id) / "flows"
    if not flows_dir.is_dir():
        return []
    return sorted(p.stem for p in flows_dir.glob("*.md") if p.is_file())


def upsert_flow_card(
    app_id: str,
    flow_id: str,
    *,
    target_url: str = "",
    recording_path: str = "",
    k6_path: str = "",
    ir_path: str = "",
    txn_names: Optional[List[str]] = None,
    workload_source: str = "",
    smoke_status: str = "",
    step_count: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write / overwrite a flow knowledge card and reindex it in Chroma.

    Returns:
        Absolute path to the markdown file.
    """
    app = (app_id or "").strip()
    flow = slug_flow(flow_id) or (flow_id or "default").strip()
    if not app:
        raise ValueError("app_id is required")
    if not flow:
        flow = "default"

    ensure_app_dirs(app)
    path = knowledge_dir(app) / "flows" / f"{flow}.md"
    path = _jail_knowledge(path)

    txns = [str(t) for t in (txn_names or []) if t]
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        f"# Flow: {flow}",
        "",
        f"- **App:** `{app}`",
        f"- **Flow:** `{flow}`",
        f"- **Target URL:** `{target_url or 'n/a'}`",
        f"- **Updated:** `{now}`",
        "",
        "## Artifacts",
        "",
        f"- Recording: `{recording_path or 'n/a'}`",
        f"- k6 script: `{k6_path or 'n/a'}`",
        f"- Load-test IR: `{ir_path or 'n/a'}`",
        "",
        "## Transactions",
        "",
    ]
    if txns:
        for name in txns:
            lines.append(f"- `{name}`")
    else:
        lines.append("- _(none recorded)_")
    lines.extend(
        [
            "",
            "## Run status",
            "",
            f"- Workload source: `{workload_source or 'n/a'}`",
            f"- Last smoke: `{smoke_status or 'n/a'}`",
        ]
    )
    if step_count is not None:
        lines.append(f"- Step count: `{step_count}`")
    if extra:
        lines.extend(["", "## Notes", ""])
        for key, value in extra.items():
            lines.append(f"- **{key}:** {value}")
    lines.append("")
    text = "\n".join(lines)
    path.write_text(text, encoding="utf-8")
    logger.info("Upserted knowledge flow card → %s", path)

    try:
        from src.utils.rag_store import upsert_markdown

        upsert_markdown(
            app,
            flow=flow,
            kind="flow",
            text=text,
            path=str(path),
        )
    except Exception as exc:
        logger.warning("RAG upsert after knowledge write skipped: %s", exc)

    return path


def runs_dir(app_id: str) -> Path:
    """Return ``artifacts/knowledge/<app>/runs/`` (created lazily)."""
    d = knowledge_dir(app_id) / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ingest_run_history(
    app_id: str,
    flow_id: str,
    *,
    kpis: Optional[Dict[str, Any]] = None,
    smoke: Optional[Dict[str, Any]] = None,
    workload_source: str = "",
    k6_path: str = "",
    summary_json: str = "",
    confluence_url: str = "",
    target_url: str = "",
) -> Path:
    """Write a run-history markdown card and upsert it into Chroma.

    Returns:
        Absolute path to the markdown file.
    """
    from src.security.secrets import redact_text_for_llm
    from src.utils.perf_trend import extract_kpis_from_smoke

    app = (app_id or "").strip()
    flow = slug_flow(flow_id) or (flow_id or "default").strip() or "default"
    if not app:
        raise ValueError("app_id is required")

    merged: Dict[str, Any] = {}
    if smoke:
        merged.update(extract_kpis_from_smoke(smoke))
    if kpis:
        merged.update({k: v for k, v in kpis.items() if v is not None})
    if workload_source:
        merged["workload_source"] = workload_source
    if k6_path:
        merged["k6_path"] = k6_path
    if summary_json:
        merged["summary_json"] = summary_json
        from src.utils.perf_trend import extract_kpis_from_summary_json

        merged.update(extract_kpis_from_summary_json(summary_json))
    if confluence_url:
        merged["confluence_url"] = confluence_url

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"{flow}_{ts}"
    merged.setdefault("run_id", run_id)
    merged.setdefault("timestamp", now.isoformat())
    merged.setdefault("source", merged.get("source") or "ingest")

    ensure_app_dirs(app)
    path = runs_dir(app) / f"{run_id}.md"
    path = _jail_knowledge(path)

    smoke_ok = merged.get("smoke_ok")
    lines = [
        f"# Run: {run_id}",
        "",
        f"- **App:** `{app}`",
        f"- **Flow:** `{flow}`",
        f"- **Target URL:** `{target_url or 'n/a'}`",
        f"- **Run id:** `{merged.get('run_id')}`",
        f"- **Timestamp:** `{merged.get('timestamp')}`",
        f"- **Smoke ok:** `{smoke_ok}`",
        f"- **p95_ms:** `{merged.get('p95_ms', 'n/a')}`",
        f"- **fail_rate:** `{merged.get('fail_rate', 'n/a')}`",
        f"- **checks_rate:** `{merged.get('checks_rate', 'n/a')}`",
        f"- **http_reqs:** `{merged.get('http_reqs', 'n/a')}`",
        f"- **Workload source:** `{merged.get('workload_source', 'n/a')}`",
        f"- **k6:** `{merged.get('k6_path', 'n/a')}`",
        f"- **summary_json:** `{merged.get('summary_json', 'n/a')}`",
        f"- **Confluence:** {merged.get('confluence_url') or 'n/a'}",
        f"- **Summary:** {merged.get('summary') or 'n/a'}",
        "",
    ]
    text = redact_text_for_llm("\n".join(lines))
    path.write_text(text, encoding="utf-8")
    logger.info("Ingested run history → %s", path)

    # Update flow card latest KPI line when card exists / via soft upsert notes
    try:
        existing = read_flow(app, flow)
        if existing and "## Latest KPIs" not in existing:
            # leave card as-is; next upsert_flow_card from pipeline may refresh
            pass
        card_path = knowledge_dir(app) / "flows" / f"{flow}.md"
        if card_path.is_file():
            card_path = _jail_knowledge(card_path)
            body = card_path.read_text(encoding="utf-8")
            kpi_block = (
                "\n## Latest KPIs\n\n"
                f"- p95_ms: `{merged.get('p95_ms', 'n/a')}`\n"
                f"- fail_rate: `{merged.get('fail_rate', 'n/a')}`\n"
                f"- smoke_ok: `{smoke_ok}`\n"
                f"- last_run: `{run_id}`\n"
            )
            if "## Latest KPIs" in body:
                body = re.sub(
                    r"\n## Latest KPIs\n.*?(?=\n## |\Z)",
                    kpi_block,
                    body,
                    count=1,
                    flags=re.S,
                )
            else:
                body = body.rstrip() + "\n" + kpi_block
            card_path.write_text(redact_text_for_llm(body), encoding="utf-8")
            try:
                from src.utils.rag_store import upsert_markdown

                upsert_markdown(
                    app,
                    flow=flow,
                    kind="flow",
                    text=card_path.read_text(encoding="utf-8"),
                    path=str(card_path),
                )
            except Exception as exc:
                logger.debug("Flow card RAG refresh skipped: %s", exc)
    except Exception as exc:
        logger.debug("Flow card KPI update skipped: %s", exc)

    try:
        from src.utils.rag_store import upsert_markdown

        # Unique flow key per run so upsert does not delete sibling run chunks
        upsert_markdown(
            app,
            flow=f"{flow}::{run_id}",
            kind="run",
            text=text,
            path=str(path),
        )
    except Exception as exc:
        logger.warning("RAG upsert after run history skipped: %s", exc)

    return path


def list_run_history(
    app_id: str,
    flow_id: str = "",
    *,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Load recent run-history KPIs for an app (newest first)."""
    from src.utils.perf_trend import parse_kpi_from_run_markdown

    app = (app_id or "").strip()
    if not app:
        return []
    flow = slug_flow(flow_id) if flow_id else ""
    d = knowledge_dir(app) / "runs"
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.md"), key=lambda p: p.name, reverse=True)
    out: List[Dict[str, Any]] = []
    for path in files:
        if flow and not path.name.startswith(f"{flow}_"):
            continue
        try:
            jailed = _jail_knowledge(path)
            text = jailed.read_text(encoding="utf-8")
            kpi = parse_kpi_from_run_markdown(text)
            kpi.setdefault("path", str(jailed))
            out.append(kpi)
        except Exception:
            continue
        if len(out) >= max(1, limit):
            break
    return out
