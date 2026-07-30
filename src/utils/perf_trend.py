"""Deterministic KPI extraction and trend tables for cache-first perf QA."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.security.fs_jail import assert_under_jail
from src.utils.app_registry import artifacts_root


KPI_KEYS = (
    "run_id",
    "timestamp",
    "smoke_ok",
    "p95_ms",
    "fail_rate",
    "checks_rate",
    "http_reqs",
    "workload_source",
    "source",
    "summary",
    "k6_path",
    "summary_json",
    "confluence_url",
    "vus",
)


def _jail_artifact(path: Path) -> Path:
    root = artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    return assert_under_jail(path, root)


def extract_kpis_from_summary_json(summary_json_path: str) -> Dict[str, Any]:
    """Load k6 summary.json and return normalized KPI fields."""
    path = (summary_json_path or "").strip()
    if not path:
        return {}
    try:
        from src.integrations.confluence.report import load_summary_metrics

        metrics = load_summary_metrics(path)
    except Exception:
        metrics = {}
        try:
            p = _jail_artifact(Path(path).expanduser().resolve())
            data = json.loads(p.read_text(encoding="utf-8"))
            m = (data.get("metrics") or {}) if isinstance(data, dict) else {}
            http_fail = (m.get("http_req_failed") or {}).get("values") or {}
            http_dur = (m.get("http_req_duration") or {}).get("values") or {}
            http_reqs = (m.get("http_reqs") or {}).get("values") or {}
            checks = (m.get("checks") or {}).get("values") or {}
            metrics = {
                "fail_rate": http_fail.get("rate"),
                "p95_ms": http_dur.get("p(95)"),
                "http_reqs": http_reqs.get("count"),
                "checks_rate": checks.get("rate"),
            }
        except Exception:
            return {}
    out: Dict[str, Any] = {
        "p95_ms": metrics.get("p95_ms"),
        "fail_rate": metrics.get("fail_rate"),
        "http_reqs": metrics.get("http_reqs"),
        "checks_rate": metrics.get("checks_rate") or metrics.get("checks"),
        "summary_json": path,
        "source": "local_summary_json",
    }
    if metrics.get("vus_max") is not None:
        try:
            out["vus"] = int(float(metrics["vus_max"]))
        except (TypeError, ValueError):
            pass
    elif metrics.get("vus") is not None:
        try:
            out["vus"] = int(float(metrics["vus"]))
        except (TypeError, ValueError):
            pass
    return {k: v for k, v in out.items() if v is not None}


def extract_kpis_from_smoke(smoke: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Normalize smoke_result (+ optional summary.json) into KPI fields."""
    smoke = smoke or {}
    kpis: Dict[str, Any] = {
        "smoke_ok": smoke.get("ok"),
        "summary": str(smoke.get("summary") or "")[:500],
        "source": "session_smoke",
    }
    summary_path = str(smoke.get("summary_json") or "")
    if summary_path:
        kpis.update(extract_kpis_from_summary_json(summary_path))
        kpis["summary_json"] = summary_path
    if kpis.get("fail_rate") is None and smoke.get("status_counts"):
        counts = smoke.get("status_counts") or {}
        try:
            total = 0
            fails = 0
            for k, v in counts.items():
                try:
                    code = int(k)
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                total += n
                if 400 <= code < 600:
                    fails += n
            if total > 0:
                kpis["fail_rate"] = fails / total
        except Exception:
            pass
    return {k: v for k, v in kpis.items() if v is not None and v != ""}


def parse_kpi_from_run_markdown(text: str) -> Dict[str, Any]:
    """Parse KPI fields from a run-history markdown card."""
    out: Dict[str, Any] = {"source": "knowledge_run"}
    patterns = {
        "run_id": r"\*\*Run id:\*\*\s*`([^`]+)`",
        "timestamp": r"\*\*Timestamp:\*\*\s*`([^`]+)`",
        "smoke_ok": r"\*\*Smoke ok:\*\*\s*`([^`]+)`",
        "p95_ms": r"\*\*p95_ms:\*\*\s*`([^`]+)`",
        "fail_rate": r"\*\*fail_rate:\*\*\s*`([^`]+)`",
        "checks_rate": r"\*\*checks_rate:\*\*\s*`([^`]+)`",
        "http_reqs": r"\*\*http_reqs:\*\*\s*`([^`]+)`",
        "workload_source": r"\*\*Workload source:\*\*\s*`([^`]+)`",
        "k6_path": r"\*\*k6:\*\*\s*`([^`]+)`",
        "summary_json": r"\*\*summary_json:\*\*\s*`([^`]+)`",
        "confluence_url": r"\*\*Confluence:\*\*\s*(\S+)",
        "summary": r"\*\*Summary:\*\*\s*(.+)",
        "vus": r"\*\*(?:VUs?|vus):\*\*\s*`([^`]+)`",
    }
    for key, pat in patterns.items():
        m = re.search(pat, text or "", re.I)
        if not m:
            continue
        raw = m.group(1).strip()
        if key == "smoke_ok":
            out[key] = raw.lower() in ("true", "1", "yes", "passed", "ok")
        elif key in ("p95_ms", "fail_rate", "checks_rate"):
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw
        elif key in ("http_reqs", "vus"):
            try:
                out[key] = int(float(raw))
            except ValueError:
                out[key] = raw
        else:
            out[key] = raw
    return out


def build_trend_table(kpis: List[Dict[str, Any]], *, limit: int = 10) -> str:
    """Render a compact markdown trend table + latest-vs-previous deltas."""
    rows = [k for k in kpis if isinstance(k, dict)][:limit]
    if not rows:
        return "_No run KPI history available locally._"

    lines = [
        "| Run | When | Smoke | p95 (ms) | Fail rate | VUs | Source |",
        "|-----|------|-------|----------|-----------|-----|--------|",
    ]
    for k in rows:
        run = str(k.get("run_id") or "?")[:24]
        when = str(k.get("timestamp") or "")[:19]
        smoke = k.get("smoke_ok")
        smoke_s = "pass" if smoke is True else ("fail" if smoke is False else "n/a")
        p95 = k.get("p95_ms")
        p95_s = f"{float(p95):.1f}" if isinstance(p95, (int, float)) else "n/a"
        fr = k.get("fail_rate")
        fr_s = f"{float(fr) * 100:.2f}%" if isinstance(fr, (int, float)) else "n/a"
        vus = k.get("vus")
        vus_s = str(int(vus)) if isinstance(vus, (int, float)) else "n/a"
        src = str(k.get("source") or "")[:20]
        lines.append(
            f"| `{run}` | {when} | {smoke_s} | {p95_s} | {fr_s} | {vus_s} | {src} |"
        )

    if len(rows) >= 2:
        a, b = rows[0], rows[1]
        deltas: List[str] = []
        for label, key in (("p95_ms", "p95_ms"), ("fail_rate", "fail_rate")):
            va, vb = a.get(key), b.get(key)
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
                diff = float(va) - float(vb)
                if key == "fail_rate":
                    deltas.append(f"{label}: {diff * 100:+.2f} pp vs previous")
                else:
                    deltas.append(f"{label}: {diff:+.1f} vs previous")
        if deltas:
            lines.extend(["", "**Latest vs previous:** " + "; ".join(deltas)])
    return "\n".join(lines)


def wants_confluence_evidence(question: str) -> bool:
    """True when the user wants Confluence-backed evidence / refresh."""
    q = (question or "").lower()
    if re.search(r"conflue\w*|confluen\w*|cofnlue\w*", q):
        return True
    return bool(
        re.search(
            r"\b("
            r"from\s+confluence|based\s+on\s+(that\s+)?confluence|"
            r"retrieve\s+.{0,40}confluence|pull\s+.{0,40}confluence|"
            r"sync\s+.{0,20}confluence|refresh\s+.{0,20}confluence|"
            r"confluence\s+data|confluence\s+trend"
            r")\b",
            q,
        )
    )


def wants_trend_question(question: str) -> bool:
    q = (question or "").lower()
    if wants_confluence_evidence(q):
        return True
    return bool(
        re.search(
            r"\b(trend|history|compare\s+runs?|last\s+\d+\s+runs?|"
            r"over\s+time|trend\s+analysis|trend\s+report|"
            r"from\s+confluence|refresh\s+from|"
            r"monitoring|grafana|datadog|influx)\b",
            q,
        )
    )


def wants_tool_refresh(question: str) -> bool:
    q = (question or "").lower()
    if wants_confluence_evidence(q):
        return True
    return bool(
        re.search(
            r"\b(from\s+confluence|refresh\s+from|pull\s+from\s+confluence|"
            r"sync\s+(confluence|monitoring)|from\s+monitoring|"
            r"from\s+grafana|from\s+datadog|"
            r"retrieve\s+.{0,40}(data|results?)\b)",
            q,
        )
    )


def parse_trend_filters(question: str) -> Dict[str, Any]:
    """Parse exclude_smoke / min_vus from natural language."""
    q = (question or "").lower()
    exclude_smoke = bool(
        re.search(
            r"\b(exclude|ignore|skip|without|do\s+not\s+consider|dont\s+consider|"
            r"don't\s+consider)\b.{0,40}\bsmoke\b|"
            r"\bnon[\s-]?smoke\b|\bnot\s+smoke\b",
            q,
        )
    )
    min_vus = 0
    m = re.search(
        r"\b(?:user|users|vu|vus)\s*[:=]?\s*(\d+)\b|"
        r"\b(\d+)\s*(?:user|users|vu|vus)\b",
        q,
    )
    if m:
        raw = m.group(1) or m.group(2)
        try:
            min_vus = int(raw)
        except ValueError:
            min_vus = 0
    return {"exclude_smoke": exclude_smoke, "min_vus": min_vus}


def filter_kpi_rows(
    kpis: List[Dict[str, Any]],
    *,
    exclude_smoke: bool = False,
    min_vus: int = 0,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Filter KPI rows; return (kept, notes)."""
    notes: List[str] = []
    rows = [k for k in kpis if isinstance(k, dict)]
    if exclude_smoke:
        before = len(rows)
        smoke_sources = {
            "session_smoke",
            "default_smoke",
            "analyse",
            "deferred_to_jira_workload_run",
        }
        kept = []
        for k in rows:
            ws = str(k.get("workload_source") or "").lower()
            src = str(k.get("source") or "").lower()
            summary = str(k.get("summary") or "").lower()
            if (
                ws in smoke_sources
                or src == "session_smoke"
                or "smoke" in ws
                or ws.endswith("_smoke")
            ):
                continue
            if "deferred" in summary:
                continue
            kept.append(k)
        rows = kept
        notes.append(f"Excluded smoke/deferred runs ({before - len(rows)} dropped).")

    if min_vus > 0:
        with_vus = [k for k in rows if isinstance(k.get("vus"), (int, float))]
        matched = [k for k in with_vus if int(k["vus"]) >= min_vus]
        unknown = [k for k in rows if not isinstance(k.get("vus"), (int, float))]
        if matched:
            rows = matched
            notes.append(f"Filtered to VUs >= {min_vus} ({len(rows)} run(s)).")
            if unknown:
                notes.append(
                    f"{len(unknown)} run(s) lacked VU tags and were omitted."
                )
        else:
            notes.append(
                f"No runs with VUs >= {min_vus} "
                f"({len(with_vus)} had VU tags, {len(unknown)} lacked VU tags). "
                "Showing available synced rows; publish with VU metadata for stricter filters."
            )
    return rows, notes
