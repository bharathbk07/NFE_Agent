"""Deterministic Confluence storage-format report bodies (no LLM).

Mirrors the structure of the NFE HTML k6 report: KPIs, observations,
full TXN / request / failed-request tables, and SLA with coloured status.
"""

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
        ms = float(value)
    except (TypeError, ValueError):
        return str(value)
    if ms >= 1000:
        return f"{ms / 1000:.2f} s"
    return f"{ms:.1f} ms"


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
    vus = (metrics.get("vus") or {}).get("values") or {}
    vus_max = (metrics.get("vus_max") or {}).get("values") or {}
    return {
        "http_reqs": http_reqs.get("count"),
        "tps": http_reqs.get("rate"),
        "fail_rate": http_fail.get("rate"),
        "p95_ms": http_dur.get("p(95)"),
        "avg_ms": http_dur.get("avg"),
        "max_ms": http_dur.get("max"),
        "iterations": iterations.get("count"),
        "checks_passes": checks.get("passes"),
        "checks_fails": checks.get("fails"),
        "duration_ms": state.get("testRunDurationMs"),
        "vus": vus.get("value") if "value" in vus else vus.get("max"),
        "vus_max": vus_max.get("value") if "value" in vus_max else vus_max.get("max"),
    }


def threshold_rows(summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not summary:
        return rows
    metrics = summary.get("metrics") or {}
    for name, metric in metrics.items():
        thresholds = (metric or {}).get("thresholds") or {}
        values = (metric or {}).get("values") or {}
        for rule, info in thresholds.items():
            rows.append(
                {
                    "metric": name,
                    "threshold": rule,
                    "ok": bool((info or {}).get("ok")),
                    "values": values,
                }
            )
    return rows


def planned_vus(workload: Optional[Dict[str, Any]]) -> Optional[int]:
    """Peak planned VUs from story/IR workload (vus or stages)."""
    wl = workload or {}
    if wl.get("vus") is not None:
        try:
            return int(wl["vus"])
        except (TypeError, ValueError):
            pass
    stages = wl.get("stages")
    if isinstance(stages, list) and stages:
        peak = 0
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            try:
                peak = max(peak, int(stage.get("target") or 0))
            except (TypeError, ValueError):
                continue
        return peak or None
    return None


def format_workload_model(workload: Optional[Dict[str, Any]]) -> str:
    """One-line workload description for reports."""
    wl = workload or {}
    if not wl:
        return "default smoke (1 VU × 2 iterations)"
    parts: List[str] = []
    if wl.get("executor"):
        parts.append(f"executor={wl['executor']}")
    vus = planned_vus(wl)
    if vus is not None:
        parts.append(f"vus={vus}")
    if wl.get("iterations") is not None:
        parts.append(f"iterations={wl['iterations']}")
    if wl.get("duration"):
        parts.append(f"duration={wl['duration']}")
    elif wl.get("maxDuration"):
        parts.append(f"maxDuration={wl['maxDuration']}")
    if isinstance(wl.get("stages"), list) and wl["stages"]:
        parts.append(f"stages={len(wl['stages'])}")
    return ", ".join(parts) if parts else "custom workload"


def resolve_run_status(
    *,
    smoke_ok: Optional[bool],
    summary: Optional[Dict[str, Any]],
    aborted_by_watcher: bool = False,
    status_counts: Optional[Dict[str, Any]] = None,
) -> str:
    """Human status label for a fully completed run."""
    if aborted_by_watcher:
        return "COMPLETED — WATCHER STOPPED"
    thr = threshold_rows(summary)
    if thr and any(not r["ok"] for r in thr):
        return "COMPLETED — SLA FAILED"
    if smoke_ok:
        return "PASSED"
    # Distinguish script/check issues from inconclusive
    counts = status_counts or {}
    if any(str(c).startswith("4") for c in counts):
        return "FAILED — SCRIPT / 4xx"
    if thr:
        return "COMPLETED — CHECKS/SCRIPT ISSUES"
    if smoke_ok is None:
        return "COMPLETED — NO SLA"
    return "FAILED — CHECKS/SCRIPT ISSUES"


def _status_lozenge(title: str, *, colour: str) -> str:
    """Confluence status macro (Green / Yellow / Red / Blue / Grey)."""
    return (
        '<ac:structured-macro ac:name="status">'
        f'<ac:parameter ac:name="colour">{_esc(colour)}</ac:parameter>'
        f'<ac:parameter ac:name="title">{_esc(title)}</ac:parameter>'
        f'<ac:parameter ac:name="subtle">false</ac:parameter>'
        "</ac:structured-macro>"
    )


def _status_colour(status: str) -> str:
    s = (status or "").upper()
    if s.startswith("PASSED") or s == "PASS":
        return "Green"
    if "SLA FAILED" in s or "WATCHER" in s:
        return "Yellow"
    if "FAIL" in s or "SCRIPT" in s or "4XX" in s:
        return "Red"
    return "Blue"


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


def _cell(text: Any, *, bad: bool = False) -> str:
    style = ' style="color:#991b1b;font-weight:700"' if bad else ""
    return f"<td{style}>{text}</td>"


def _load_points(points_json: str) -> List[Dict[str, Any]]:
    path = (points_json or "").strip()
    if not path:
        return []
    try:
        from src.utils.k6_report_builder import load_k6_json_points

        return list(load_k6_json_points(Path(path)))
    except Exception:
        return []


def _observation_notes(
    *,
    status: str,
    flow_name: str,
    metrics: Dict[str, Any],
    txn_count: int,
    req_count: int,
    failed_bucket_count: int,
    sla_fail: int,
    smoke_summary: str,
) -> List[str]:
    notes = [
        (
            f"Script/flow <strong>{_esc(flow_name)}</strong> finished with status "
            f"<strong>{_esc(status)}</strong>."
        ),
        (
            f"Observed <strong>{txn_count}</strong> TXN(s) and "
            f"<strong>{req_count}</strong> distinct request bucket(s)."
        ),
        (
            f"HTTP error rate <strong>{_esc(_fmt_pct(metrics.get('fail_rate')))}</strong>; "
            f"p95 latency <strong>{_esc(_fmt_ms(metrics.get('p95_ms')))}</strong>."
        ),
    ]
    if failed_bucket_count:
        notes.append(
            f"<strong>{failed_bucket_count}</strong> failed request bucket(s) "
            "(see Failed request list)."
        )
    if sla_fail:
        notes.append(f"<strong>{sla_fail}</strong> SLA threshold(s) failed.")
    else:
        notes.append("All SLA thresholds passed (or none defined).")
    if smoke_summary:
        notes.append(f"Run notes: {_esc(smoke_summary)}")
    return notes


def build_run_storage_body(
    *,
    status: str,
    flow_name: str,
    target_url: str = "",
    jira_issue_key: str = "",
    timestamp: str = "",
    smoke_summary: str = "",
    summary_json: str = "",
    points_json: str = "",
    transactions: Optional[List[Dict[str, Any]]] = None,
    heal_notes: Optional[List[str]] = None,
    failed_checks: Optional[List[str]] = None,
    failed_urls: Optional[List[str]] = None,
    status_counts: Optional[Dict[str, Any]] = None,
    exit_code: Optional[int] = None,
    attachment_names: Optional[List[str]] = None,
    workload: Optional[Dict[str, Any]] = None,
    workload_source: str = "",
) -> str:
    """Build Confluence storage XHTML mirroring the HTML k6 report."""
    summary = load_summary(summary_json)
    metrics = load_summary_metrics(summary_json)
    thr = threshold_rows(summary)
    status_counts = status_counts or {}
    status_line = (
        ", ".join(f"{code}×{count}" for code, count in sorted(status_counts.items()))
        if status_counts
        else "n/a"
    )
    planned = planned_vus(workload)
    actual_vus = metrics.get("vus_max") or metrics.get("vus")
    wl_model = format_workload_model(workload)
    src_label = workload_source or (
        "jira_story" if workload else "default_smoke"
    )

    points = _load_points(points_json)
    txn_list: List[Dict[str, Any]] = []
    req_list: List[Dict[str, Any]] = []
    failed_rows: List[Dict[str, Any]] = []
    if points:
        try:
            from src.utils.k6_report_builder import _aggregate_points

            txns, reqs, failed_rows = _aggregate_points(points)
            txn_list = sorted(txns.values(), key=lambda r: r["name"])
            req_list = sorted(
                reqs.values(),
                key=lambda r: (r["txn"], r["method"], r["url"]),
            )
        except Exception:
            txn_list, req_list, failed_rows = [], [], []

    # Fallback TXN names from IR when points missing
    if not txn_list and transactions:
        for t in transactions[:40]:
            txn_list.append(
                {
                    "name": t.get("name") or t.get("id") or "?",
                    "min": None,
                    "max": None,
                    "avg": None,
                    "count": None,
                    "failed": None,
                    "req_fails": None,
                    "p50": None,
                    "p90": None,
                    "p95": None,
                    "p99": None,
                }
            )

    sla_fail = sum(1 for r in thr if not r["ok"])
    colour = _status_colour(status)
    pill = _status_lozenge(status, colour=colour)

    # KPI strip (table of highlight cells)
    fail_rate = metrics.get("fail_rate")
    fail_bad = bool(fail_rate is not None and float(fail_rate or 0) > 0.01)
    kpi = (
        '<table data-layout="wide"><tbody><tr>'
        f"<td><p><strong>Duration</strong></p><p>{_esc(_fmt_ms(metrics.get('duration_ms')))}</p></td>"
        f"<td><p><strong>HTTP reqs</strong></p><p>{_esc(_fmt_num(metrics.get('http_reqs'), digits=0))}</p></td>"
        f"<td><p><strong>Iterations</strong></p><p>{_esc(_fmt_num(metrics.get('iterations'), digits=0))}</p></td>"
        f"<td><p><strong>Error rate</strong></p>"
        f"<p{(' style=\"color:#991b1b;font-weight:700\"' if fail_bad else '')}>"
        f"{_esc(_fmt_pct(fail_rate))}</p></td>"
        f"<td><p><strong>p95 latency</strong></p><p>{_esc(_fmt_ms(metrics.get('p95_ms')))}</p></td>"
        f"<td><p><strong>Failed buckets</strong></p>"
        f"<p{(' style=\"color:#991b1b;font-weight:700\"' if failed_rows else '')}>"
        f"{len(failed_rows)}</p></td>"
        f"<td><p><strong>TPS</strong></p><p>{_esc(_fmt_num(metrics.get('tps'), digits=2))} req/s</p></td>"
        f"<td><p><strong>VUs (plan/max)</strong></p>"
        f"<p>{_esc(_fmt_num(planned, digits=0))} / {_esc(_fmt_num(actual_vus, digits=0))}</p></td>"
        "</tr></tbody></table>"
    )

    notes = _observation_notes(
        status=status,
        flow_name=flow_name,
        metrics=metrics,
        txn_count=len(txn_list),
        req_count=len(req_list),
        failed_bucket_count=len(failed_rows),
        sla_fail=sla_fail,
        smoke_summary=smoke_summary,
    )
    notes_html = "<ul>" + "".join(f"<li>{n}</li>" for n in notes) + "</ul>"

    meta = _kv_table(
        [
            ("Status", pill),
            ("Flow", f"<code>{_esc(flow_name)}</code>"),
            ("Target", f"<code>{_esc(target_url or 'n/a')}</code>"),
            ("Jira", f"<code>{_esc(jira_issue_key or 'n/a')}</code>"),
            ("Timestamp", _esc(timestamp or "n/a")),
            ("Workload model", f"<code>{_esc(wl_model)}</code>"),
            ("Workload source", f"<code>{_esc(src_label)}</code>"),
            ("HTTP status counts", f"<code>{_esc(status_line)}</code>"),
            ("Exit code", _esc(exit_code if exit_code is not None else "n/a")),
        ]
    )

    # TXN table
    if txn_list:
        txn_body = []
        for i, r in enumerate(txn_list, 1):
            failed = r.get("failed") or 0
            req_fails = r.get("req_fails") or 0
            try:
                failed_n = int(failed)
            except (TypeError, ValueError):
                failed_n = 0
            try:
                req_fails_n = int(req_fails)
            except (TypeError, ValueError):
                req_fails_n = 0
            txn_body.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td><code>{_esc(r.get('name'))}</code></td>"
                f"<td>{_esc(_fmt_ms(r.get('min')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('max')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('avg')))}</td>"
                f"<td>{_esc(_fmt_num(r.get('count'), digits=0))}</td>"
                + _cell(_esc(_fmt_num(failed, digits=0)), bad=failed_n > 0)
                + _cell(_esc(_fmt_num(req_fails, digits=0)), bad=req_fails_n > 0)
                + f"<td>{_esc(_fmt_ms(r.get('p50')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('p90')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('p95')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('p99')))}</td>"
                "</tr>"
            )
        txn_table = (
            '<table data-layout="full-width"><thead><tr>'
            "<th>Si.No</th><th>TXN name</th><th>Min</th><th>Max</th><th>Avg</th>"
            "<th>Count</th><th>Failed iters</th><th>Req fails</th>"
            "<th>p50</th><th>p90</th><th>p95</th><th>p99</th>"
            "</tr></thead><tbody>"
            + "".join(txn_body)
            + "</tbody></table>"
            "<p><em>Count = TXN iterations. Failed iters = iterations where ≥1 request "
            "failed. Req fails = raw request failures inside the TXN.</em></p>"
        )
    else:
        txn_table = "<p><em>No transaction samples recorded.</em></p>"

    # Request table
    if req_list:
        req_body = []
        for i, r in enumerate(req_list[:80], 1):
            failed = int(r.get("failed") or 0)
            req_body.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{_esc(r.get('txn'))}</td>"
                f"<td><code>{_esc(r.get('method'))}</code></td>"
                f"<td><code>{_esc(r.get('url'))}</code></td>"
                f"<td>{_esc(_fmt_ms(r.get('min')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('avg')))}</td>"
                f"<td>{_esc(_fmt_ms(r.get('max')))}</td>"
                f"<td>{_esc(_fmt_num(r.get('count'), digits=0))}</td>"
                + _cell(_esc(_fmt_num(failed, digits=0)), bad=failed > 0)
                + _cell(_esc(_fmt_pct(r.get("fail_pct"))), bad=failed > 0)
                + "</tr>"
            )
        req_table = (
            '<table data-layout="full-width"><thead><tr>'
            "<th>Si.No</th><th>TXN</th><th>Method</th><th>URL</th>"
            "<th>Min</th><th>Avg</th><th>Max</th>"
            "<th>Count</th><th>Failed</th><th>Failed %</th>"
            "</tr></thead><tbody>"
            + "".join(req_body)
            + "</tbody></table>"
        )
    else:
        req_table = "<p><em>No per-request samples recorded.</em></p>"

    # Failed request list
    if failed_rows:
        fail_body = []
        for i, r in enumerate(failed_rows[:40], 1):
            fail_body.append(
                "<tr>"
                f"<td>{i}</td>"
                f"<td>{_esc(r.get('txn'))}</td>"
                f"<td><code>{_esc(r.get('method'))}</code></td>"
                f"<td><code>{_esc(r.get('url'))}</code></td>"
                + _cell(f"<code>{_esc(r.get('status'))}</code>", bad=True)
                + _cell(_esc(_fmt_num(r.get("failed"), digits=0)), bad=True)
                + f"<td>{_esc(_fmt_num(r.get('total'), digits=0))}</td>"
                + _cell(
                    _esc(
                        _fmt_pct(
                            (r.get("failed") or 0) / (r.get("total") or 1)
                            if r.get("total")
                            else 0
                        )
                    ),
                    bad=True,
                )
                + "</tr>"
            )
        fail_table = (
            '<table data-layout="full-width"><thead><tr>'
            "<th>Si.No</th><th>TXN</th><th>Method</th><th>URL</th>"
            "<th>Status</th><th>Failed</th><th>Total</th><th>Failed %</th>"
            "</tr></thead><tbody>"
            + "".join(fail_body)
            + "</tbody></table>"
        )
    elif failed_urls:
        fail_table = _ul([str(u) for u in failed_urls[:40]])
    else:
        fail_table = "<p><em>None</em></p>"

    # SLA table
    if thr:
        thr_rows = []
        for r in thr:
            ok = bool(r.get("ok"))
            result = _status_lozenge("PASS", colour="Green") if ok else _status_lozenge(
                "FAIL", colour="Red"
            )
            values = r.get("values") or {}
            obs = ""
            if values.get("rate") is not None and "p(95)" not in values:
                obs = f"rate={float(values['rate']) * 100:.2f}%"
            elif values.get("p(95)") is not None:
                obs = (
                    f"p95={_fmt_ms(values.get('p(95)'))}, "
                    f"avg={_fmt_ms(values.get('avg'))}, "
                    f"max={_fmt_ms(values.get('max'))}"
                )
            thr_rows.append(
                "<tr>"
                f"<td><code>{_esc(r['metric'])}</code></td>"
                f"<td><code>{_esc(r['threshold'])}</code></td>"
                f"<td>{_esc(obs or 'n/a')}</td>"
                f"<td>{result}</td>"
                "</tr>"
            )
        sla_html = (
            '<table data-layout="default"><thead>'
            "<tr><th>Metric</th><th>Threshold</th><th>Observed</th><th>Result</th></tr>"
            f"</thead><tbody>{''.join(thr_rows)}</tbody></table>"
        )
    else:
        sla_html = "<p><em>No thresholds defined.</em></p>"

    heal_items = [str(n) for n in (heal_notes or [])[:20]]
    attach_items = [str(a) for a in (attachment_names or [])]

    parts = [
        "<h1>NFE Agent — Performance Test Report</h1>",
        f"<p>Overall: {pill}</p>",
        "<h2>1. KPIs</h2>",
        kpi,
        "<h2>2. General test details</h2>",
        meta,
        "<h2>3. Test observation</h2>",
        notes_html,
        "<h2>4. Full transaction table</h2>",
        txn_table,
        "<h2>5. Full request table</h2>",
        req_table,
        "<h2>6. Failed request list</h2>",
        fail_table,
        "<h2>7. SLA details (thresholds)</h2>",
        sla_html,
        "<h2>8. Failed checks</h2>",
        _ul([str(c) for c in (failed_checks or [])[:25]]),
        "<h2>9. Heal notes</h2>",
        _ul(heal_items),
        "<h2>10. Attachments</h2>",
        (
            "<p>See attachments on this page (k6 script, HTML report, IR). "
            "The HTML attachment is the full visual twin of this page.</p>"
            + _ul(attach_items)
            if attach_items
            else "<p><em>Uploading…</em></p>"
        ),
        "<p><em>Generated automatically by NFE Agent (no LLM). "
        "Layout mirrors the local HTML k6 report.</em></p>",
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
    pill = _status_lozenge(status, colour=_status_colour(status))
    body = f"""
<h1>{_esc(flow_name)}</h1>
<p>Latest NFE performance run for this user flow.</p>
{_kv_table([
    ("Latest status", pill),
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
