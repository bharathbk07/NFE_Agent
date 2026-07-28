"""Deterministic Confluence storage-format report bodies (no LLM)."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.integrations.confluence.security import sanitize_storage


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_num(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(num - int(num)) < 1e-9:
        return str(int(num))
    return f"{num:.{digits}f}"


def _fmt_pct(rate: Any) -> str:
    if rate is None:
        return "n/a"
    try:
        return f"{float(rate) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(rate)


def _fmt_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f} ms"
    except (TypeError, ValueError):
        return str(value)


def load_summary(summary_json_path: str) -> Dict[str, Any]:
    path = (summary_json_path or "").strip()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_summary_metrics(summary_json_path: str) -> Dict[str, Any]:
    data = load_summary(summary_json_path)
    metrics = data.get("metrics") or {}
    state = data.get("state") or {}
    http_fail = (metrics.get("http_req_failed") or {}).get("values") or {}
    http_dur = (metrics.get("http_req_duration") or {}).get("values") or {}
    http_reqs = (metrics.get("http_reqs") or {}).get("values") or {}
    iterations = (metrics.get("iterations") or {}).get("values") or {}
    checks = (metrics.get("checks") or {}).get("values") or {}
    return {
        "http_reqs": http_reqs.get("count"),
        "fail_rate": http_fail.get("rate"),
        "p95_ms": http_dur.get("p(95)"),
        "avg_ms": http_dur.get("avg"),
        "max_ms": http_dur.get("max"),
        "iterations": iterations.get("count"),
        "checks_passes": checks.get("passes"),
        "checks_fails": checks.get("fails"),
        "duration_ms": state.get("testRunDurationMs"),
    }


def threshold_rows(summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not summary:
        return rows
    metrics = summary.get("metrics") or {}
    for name, metric in metrics.items():
        thresholds = (metric or {}).get("thresholds") or {}
        for rule, info in thresholds.items():
            rows.append(
                {
                    "metric": name,
                    "threshold": rule,
                    "ok": bool((info or {}).get("ok")),
                }
            )
    return rows


def resolve_run_status(
    *,
    smoke_ok: Optional[bool],
    summary: Optional[Dict[str, Any]],
) -> str:
    """Human status label for a fully completed run."""
    thr = threshold_rows(summary)
    if thr:
        if any(not r["ok"] for r in thr):
            return "COMPLETED — SLA FAILED"
        if smoke_ok:
            return "PASSED"
        return "COMPLETED — CHECKS/SCRIPT ISSUES"
    if smoke_ok:
        return "PASSED"
    if smoke_ok is None:
        return "COMPLETED — NO SLA"
    return "COMPLETED — CHECKS/SCRIPT ISSUES"


def _ul(items: Sequence[str]) -> str:
    if not items:
        return "<p><em>None</em></p>"
    lis = "".join(f"<li><code>{_esc(i)}</code></li>" for i in items)
    return f"<ul>{lis}</ul>"


def _kv_table(rows: Sequence[tuple[str, str]]) -> str:
    body = "".join(
        f"<tr><th>{_esc(k)}</th><td>{v}</td></tr>" for k, v in rows
    )
    return (
        '<table data-layout="default">'
        f"<tbody>{body}</tbody></table>"
    )


def build_run_storage_body(
    *,
    status: str,
    flow_name: str,
    target_url: str = "",
    jira_issue_key: str = "",
    timestamp: str = "",
    smoke_summary: str = "",
    summary_json: str = "",
    transactions: Optional[List[Dict[str, Any]]] = None,
    heal_notes: Optional[List[str]] = None,
    failed_checks: Optional[List[str]] = None,
    failed_urls: Optional[List[str]] = None,
    status_counts: Optional[Dict[str, Any]] = None,
    exit_code: Optional[int] = None,
    attachment_names: Optional[List[str]] = None,
) -> str:
    """Build Confluence storage XHTML for a dated run page."""
    summary = load_summary(summary_json)
    metrics = load_summary_metrics(summary_json)
    thr = threshold_rows(summary)
    status_counts = status_counts or {}
    status_line = (
        ", ".join(f"{code}×{count}" for code, count in sorted(status_counts.items()))
        if status_counts
        else "n/a"
    )

    meta = _kv_table(
        [
            ("Status", f"<strong>{_esc(status)}</strong>"),
            ("Flow", f"<code>{_esc(flow_name)}</code>"),
            ("Target", f"<code>{_esc(target_url or 'n/a')}</code>"),
            ("Jira", f"<code>{_esc(jira_issue_key or 'n/a')}</code>"),
            ("Timestamp", _esc(timestamp or "n/a")),
            ("Run notes", _esc(smoke_summary or "None")),
            ("Exit code", _esc(exit_code if exit_code is not None else "n/a")),
            ("HTTP status counts", f"<code>{_esc(status_line)}</code>"),
        ]
    )

    stats = _kv_table(
        [
            ("HTTP requests", f"<code>{_esc(_fmt_num(metrics.get('http_reqs'), digits=0))}</code>"),
            ("HTTP fail rate", f"<code>{_esc(_fmt_pct(metrics.get('fail_rate')))}</code>"),
            (
                "Duration p95 / avg / max",
                (
                    f"<code>{_esc(_fmt_ms(metrics.get('p95_ms')))}</code> / "
                    f"<code>{_esc(_fmt_ms(metrics.get('avg_ms')))}</code> / "
                    f"<code>{_esc(_fmt_ms(metrics.get('max_ms')))}</code>"
                ),
            ),
            ("Iterations", f"<code>{_esc(_fmt_num(metrics.get('iterations'), digits=0))}</code>"),
            (
                "Checks pass / fail",
                (
                    f"<code>{_esc(_fmt_num(metrics.get('checks_passes'), digits=0))}</code> / "
                    f"<code>{_esc(_fmt_num(metrics.get('checks_fails'), digits=0))}</code>"
                ),
            ),
            ("Test duration", f"<code>{_esc(_fmt_ms(metrics.get('duration_ms')))}</code>"),
        ]
    )

    if thr:
        thr_rows = "".join(
            "<tr>"
            f"<td><code>{_esc(r['metric'])}</code></td>"
            f"<td><code>{_esc(r['threshold'])}</code></td>"
            f"<td>{'PASS' if r['ok'] else '<strong>FAIL</strong>'}</td>"
            "</tr>"
            for r in thr
        )
        sla_html = (
            '<table data-layout="default"><thead>'
            "<tr><th>Metric</th><th>Threshold</th><th>Result</th></tr>"
            f"</thead><tbody>{thr_rows}</tbody></table>"
        )
    else:
        sla_html = "<p><em>No thresholds defined.</em></p>"

    txn_items = [
        str(t.get("name") or t.get("id") or "?")
        for t in (transactions or [])[:40]
    ]
    heal_items = [str(n) for n in (heal_notes or [])[:15]]
    attach_items = [str(a) for a in (attachment_names or [])]

    parts = [
        "<h1>NFE Agent — Performance Test Report</h1>",
        "<h2>Summary</h2>",
        meta,
        "<h2>Statistics</h2>",
        stats,
        "<h2>SLA / thresholds</h2>",
        sla_html,
        "<h2>Failed requests</h2>",
        _ul([str(u) for u in (failed_urls or [])[:25]]),
        "<h2>Failed checks</h2>",
        _ul([str(c) for c in (failed_checks or [])[:25]]),
        "<h2>Transactions</h2>",
        _ul(txn_items),
        "<h2>Heal notes</h2>",
        _ul(heal_items),
        "<h2>Attachments</h2>",
        (
            "<p>See attachments on this page (k6 script, HTML report, IR).</p>"
            + _ul(attach_items)
            if attach_items
            else "<p><em>Uploading…</em></p>"
        ),
        "<p><em>Generated automatically by NFE Agent (no LLM).</em></p>",
    ]
    return sanitize_storage("\n".join(parts))


def build_flow_latest_storage_body(
    *,
    flow_name: str,
    status: str,
    run_title: str,
    run_url: str,
    target_url: str = "",
    timestamp: str = "",
    jira_issue_key: str = "",
) -> str:
    """Living index body for the flow parent page."""
    body = f"""
<h1>{_esc(flow_name)}</h1>
<p>Latest NFE performance run for this user flow.</p>
{_kv_table([
    ("Latest status", f"<strong>{_esc(status)}</strong>"),
    ("Latest run", f'<a href="{_esc(run_url)}">{_esc(run_title)}</a>'),
    ("Target", f"<code>{_esc(target_url or 'n/a')}</code>"),
    ("Jira", f"<code>{_esc(jira_issue_key or 'n/a')}</code>"),
    ("Timestamp", _esc(timestamp or "n/a")),
])}
<p>Historical runs are child pages under this page (titled <code>Run YYYY-MM-DD HH:MM</code>).</p>
"""
    return sanitize_storage(body)


def build_placeholder_body(title: str) -> str:
    return sanitize_storage(
        f"<h1>{_esc(title)}</h1>"
        "<p>Created by NFE Agent. Run reports appear as child pages.</p>"
    )
