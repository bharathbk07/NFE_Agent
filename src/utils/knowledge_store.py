"""Markdown knowledge cards per application / flow."""

from __future__ import annotations

import logging
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
