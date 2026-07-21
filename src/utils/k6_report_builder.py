"""Build a user-facing k6 HTML report from summary + JSON point output."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union


def _pct(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def _ms(v: Optional[float]) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if v < 1:
        return f"{v:.3f} ms"
    if v < 1000:
        return f"{v:.2f} ms"
    return f"{v / 1000:.2f} s"


def _num(v: Optional[float], digits: int = 0) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _pct_rate(n: float, d: float) -> str:
    if d <= 0:
        return "—"
    return f"{(100.0 * n / d):.2f}%"


def _esc(s: Any) -> str:
    return (
        str(s if s is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _duration_human(ms: Optional[float]) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms / 1000:.2f}s"
    total = int(round(ms / 1000))
    mins, secs = divmod(total, 60)
    if mins <= 0:
        return f"{secs}s"
    return f"{mins}m {secs}s"


def load_k6_json_points(path: Path) -> List[Dict[str, Any]]:
    """Load NDJSON points from ``k6 run --out json=...``."""
    points: List[Dict[str, Any]] = []
    if not path.is_file():
        return points
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "Point":
                continue
            points.append(obj)
    return points


def _aggregate_points(
    points: Iterable[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, str, str], Dict[str, Any]], List[Dict[str, Any]]]:
    """Build TXN trends, request rows, and failed request rows from points."""
    txn_durs: Dict[str, List[float]] = defaultdict(list)
    req_durs: Dict[Tuple[str, str, str], List[float]] = defaultdict(list)
    req_count: Dict[Tuple[str, str, str], int] = defaultdict(int)
    req_fail: Dict[Tuple[str, str, str], int] = defaultdict(int)
    fail_by_status: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}

    for pt in points:
        metric = pt.get("metric") or ""
        data = pt.get("data") or {}
        tags = data.get("tags") or {}
        value = data.get("value")
        txn = str(tags.get("txn") or "")
        method = str(tags.get("method") or "")
        url = str(tags.get("url") or tags.get("name") or "")
        status = str(tags.get("status") or "")

        if metric == "nfe_txn_duration" and isinstance(value, (int, float)):
            txn_durs[txn or "unknown"].append(float(value))
        elif metric == "nfe_req_duration" and isinstance(value, (int, float)):
            key = (txn, method, url)
            req_durs[key].append(float(value))
        elif metric == "nfe_req_count" and isinstance(value, (int, float)):
            key = (txn, method, url)
            req_count[key] += int(value)
        elif metric == "nfe_req_fail" and isinstance(value, (int, float)):
            key = (txn, method, url)
            n = int(value)
            req_fail[key] += n
            sk = (txn, method, url, status or "0")
            slot = fail_by_status.setdefault(
                sk,
                {
                    "txn": txn,
                    "method": method,
                    "url": url,
                    "status": status or "0",
                    "failed": 0,
                    "total": 0,
                },
            )
            slot["failed"] += n

    # totals for fail rows
    for (txn, method, url, status), slot in fail_by_status.items():
        slot["total"] = req_count.get((txn, method, url), slot["failed"])

    txns: Dict[str, Dict[str, Any]] = {}
    for name, vals in txn_durs.items():
        vals_sorted = sorted(vals)
        failed = sum(
            req_fail[k] for k in req_fail if k[0] == name
        )
        txns[name] = {
            "name": name,
            "min": vals_sorted[0],
            "max": vals_sorted[-1],
            "avg": sum(vals_sorted) / len(vals_sorted),
            "count": len(vals_sorted),
            "failed": failed,
            "p50": _pct(vals_sorted, 50),
            "p90": _pct(vals_sorted, 90),
            "p95": _pct(vals_sorted, 95),
            "p99": _pct(vals_sorted, 99),
        }

    reqs: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    keys = set(req_durs) | set(req_count) | set(req_fail)
    for key in keys:
        vals = sorted(req_durs.get(key, []))
        count = req_count.get(key) or len(vals)
        failed = req_fail.get(key, 0)
        reqs[key] = {
            "txn": key[0],
            "method": key[1],
            "url": key[2],
            "min": vals[0] if vals else None,
            "avg": (sum(vals) / len(vals)) if vals else None,
            "max": vals[-1] if vals else None,
            "count": count,
            "failed": failed,
            "fail_pct": (failed / count) if count else 0.0,
        }

    failed_rows = sorted(
        fail_by_status.values(),
        key=lambda r: (-r["failed"], r["txn"], r["url"]),
    )
    return txns, reqs, failed_rows


def _threshold_rows(summary: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
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


def build_html_report(
    *,
    points: List[Dict[str, Any]],
    summary: Optional[Dict[str, Any]] = None,
    script_name: str = "",
) -> str:
    """Render the full HTML report string."""
    txns, reqs, failed_rows = _aggregate_points(points)
    thr = _threshold_rows(summary)
    state = (summary or {}).get("state") or {}
    duration_ms = state.get("testRunDurationMs")
    metrics = (summary or {}).get("metrics") or {}
    http_fail = (metrics.get("http_req_failed") or {}).get("values") or {}
    http_dur = (metrics.get("http_req_duration") or {}).get("values") or {}
    http_reqs = (metrics.get("http_reqs") or {}).get("values") or {}
    iterations = (metrics.get("iterations") or {}).get("values") or {}

    txn_list = sorted(txns.values(), key=lambda r: r["name"])
    req_list = sorted(
        reqs.values(),
        key=lambda r: (r["txn"], r["method"], r["url"]),
    )
    sla_fail = sum(1 for r in thr if not r["ok"])
    overall_ok = sla_fail == 0 and not failed_rows

    def thr_obs2(values: Dict[str, Any]) -> str:
        if values.get("rate") is not None and "p(95)" not in values:
            return f"rate={values['rate'] * 100:.2f}%"
        if values.get("p(95)") is not None:
            return (
                f"p95={_ms(values.get('p(95)'))}, "
                f"avg={_ms(values.get('avg'))}, max={_ms(values.get('max'))}"
            )
        if values.get("count") is not None:
            return f"count={_num(values.get('count'), 0)}"
        return _esc(json.dumps(values))

    txn_rows = []
    for i, r in enumerate(txn_list, 1):
        txn_rows.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(r['name'])}</td>"
            f"<td class='num'>{_ms(r['min'])}</td>"
            f"<td class='num'>{_ms(r['max'])}</td>"
            f"<td class='num'>{_ms(r['avg'])}</td>"
            f"<td class='num'>{_num(r['count'], 0)}</td>"
            f"<td class='num{' bad' if r['failed'] else ''}'>{_num(r['failed'], 0)}</td>"
            f"<td class='num'>{_ms(r['p50'])}</td>"
            f"<td class='num'>{_ms(r['p90'])}</td>"
            f"<td class='num'>{_ms(r['p95'])}</td>"
            f"<td class='num'>{_ms(r['p99'])}</td>"
            "</tr>"
        )
    if not txn_rows:
        txn_rows.append(
            "<tr><td colspan='11' class='muted'>No transaction samples recorded.</td></tr>"
        )

    req_rows = []
    for i, r in enumerate(req_list, 1):
        req_rows.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(r['txn'])}</td>"
            f"<td><code>{_esc(r['method'])}</code></td>"
            f"<td class='url'>{_esc(r['url'])}</td>"
            f"<td class='num'>{_ms(r['min'])}</td>"
            f"<td class='num'>{_ms(r['avg'])}</td>"
            f"<td class='num'>{_ms(r['max'])}</td>"
            f"<td class='num'>{_num(r['count'], 0)}</td>"
            f"<td class='num{' bad' if r['failed'] else ''}'>{_num(r['failed'], 0)}</td>"
            f"<td class='num{' bad' if r['failed'] else ''}'>{_pct_rate(r['failed'], r['count'])}</td>"
            "</tr>"
        )
    if not req_rows:
        req_rows.append(
            "<tr><td colspan='10' class='muted'>No request samples recorded.</td></tr>"
        )

    fail_rows_html = []
    for i, r in enumerate(failed_rows, 1):
        fail_rows_html.append(
            "<tr>"
            f"<td class='num'>{i}</td>"
            f"<td>{_esc(r['txn'])}</td>"
            f"<td><code>{_esc(r['method'])}</code></td>"
            f"<td class='url'>{_esc(r['url'])}</td>"
            f"<td class='num bad'>{_esc(r['status'])}</td>"
            f"<td class='num bad'>{_num(r['failed'], 0)}</td>"
            f"<td class='num'>{_num(r['total'], 0)}</td>"
            f"<td class='num bad'>{_pct_rate(r['failed'], r['total'])}</td>"
            "</tr>"
        )
    if not fail_rows_html:
        fail_rows_html.append(
            "<tr><td colspan='8' class='muted'>No failed requests.</td></tr>"
        )

    sla_rows = []
    for r in thr:
        sla_rows.append(
            "<tr>"
            f"<td>{_esc(r['metric'])}</td>"
            f"<td><code>{_esc(r['threshold'])}</code></td>"
            f"<td>{thr_obs2(r['values'])}</td>"
            f"<td><span class='badge {'pass' if r['ok'] else 'fail'}'>"
            f"{'PASS' if r['ok'] else 'FAIL'}</span></td>"
            "</tr>"
        )
    if not sla_rows:
        sla_rows.append(
            "<tr><td colspan='4' class='muted'>No thresholds defined.</td></tr>"
        )

    obs = [
        f"Script <strong>{_esc(script_name or 'k6')}</strong> ran for "
        f"<strong>{_esc(_duration_human(duration_ms))}</strong> with "
        f"<strong>{len(txn_list)}</strong> TXNs and "
        f"<strong>{len(req_list)}</strong> distinct requests.",
        f"HTTP error rate <strong>{(http_fail.get('rate') or 0) * 100:.2f}%</strong>; "
        f"p95 latency <strong>{_ms(http_dur.get('p(95)'))}</strong>.",
        (
            f"<strong>{len(failed_rows)}</strong> failed request bucket(s) "
            "(URL + status in section 5)."
            if failed_rows
            else "No failed requests recorded."
        ),
        (
            f"<strong>{sla_fail}</strong> SLA threshold(s) failed."
            if sla_fail
            else "All SLA thresholds passed."
        ),
    ]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NFE k6 Test Report</title>
<style>
:root {{ --ink:#111827; --muted:#6b7280; --line:#e5e7eb; --bg:#f3f4f6; --card:#fff; --pass:#166534; --fail:#991b1b; }}
* {{ box-sizing: border-box; }}
body {{ margin:0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:var(--bg); color:var(--ink); line-height:1.45; }}
header {{ background: linear-gradient(120deg,#0f766e,#115e59 55%,#134e4a); color:#ecfdf5; padding:28px 20px; }}
header h1 {{ margin:0 0 6px; font-size:1.6rem; }}
header p {{ margin:0; opacity:.9; }}
.wrap {{ max-width:1280px; margin:0 auto; padding:18px 14px 40px; }}
.pill {{ display:inline-block; margin-top:12px; padding:6px 12px; border-radius:999px; font-weight:700; font-size:.85rem; }}
.pill.ok {{ background:#bbf7d0; color:#14532d; }}
.pill.bad {{ background:#fecaca; color:#7f1d1d; }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin:14px 0; }}
.kpi {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; }}
.kpi .l {{ font-size:.72rem; text-transform:uppercase; color:var(--muted); letter-spacing:.04em; }}
.kpi .v {{ font-size:1.2rem; font-weight:700; margin-top:4px; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; margin:14px 0; box-shadow:0 1px 2px rgba(0,0,0,.03); }}
h2 {{ margin:0 0 10px; font-size:1.05rem; padding-bottom:8px; border-bottom:1px solid var(--line); }}
p.note {{ margin:0 0 10px; color:var(--muted); font-size:.9rem; }}
.scroll {{ overflow:auto; border:1px solid var(--line); border-radius:8px; }}
table {{ width:100%; border-collapse:collapse; font-size:.86rem; min-width:720px; }}
th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
th {{ background:#f8fafc; position:sticky; top:0; font-size:.72rem; text-transform:uppercase; letter-spacing:.03em; color:var(--muted); white-space:nowrap; }}
td.num, .num {{ text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }}
td.url {{ max-width:420px; word-break:break-all; font-family:ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size:.8rem; }}
td.bad, .bad {{ color:var(--fail); font-weight:600; }}
.badge {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:.72rem; font-weight:700; }}
.badge.pass {{ background:#dcfce7; color:var(--pass); }}
.badge.fail {{ background:#fee2e2; color:var(--fail); }}
ul {{ margin:0; padding-left:1.2rem; }} li {{ margin:6px 0; }}
.muted {{ color:var(--muted); }}
code {{ font-family:ui-monospace, Menlo, Consolas, monospace; font-size:.8rem; }}
footer {{ text-align:center; color:var(--muted); font-size:.8rem; margin-top:18px; }}
</style>
</head>
<body>
<header><div class="wrap" style="padding:0">
  <h1>k6 Performance Test Report</h1>
  <p>NFE Agent · full TXN &amp; request tables · assertions · SLA</p>
  <div class="pill {'ok' if overall_ok else 'bad'}">Overall: {'PASS' if overall_ok else 'FAIL'}</div>
</div></header>
<div class="wrap">
  <div class="kpis">
    <div class="kpi"><div class="l">Duration</div><div class="v">{_esc(_duration_human(duration_ms))}</div></div>
    <div class="kpi"><div class="l">HTTP reqs</div><div class="v">{_num(http_reqs.get('count'), 0)}</div></div>
    <div class="kpi"><div class="l">Iterations</div><div class="v">{_num(iterations.get('count'), 0)}</div></div>
    <div class="kpi"><div class="l">Error rate</div><div class="v">{(http_fail.get('rate') or 0) * 100:.2f}%</div></div>
    <div class="kpi"><div class="l">p95 latency</div><div class="v">{_ms(http_dur.get('p(95)'))}</div></div>
    <div class="kpi"><div class="l">Failed buckets</div><div class="v">{len(failed_rows)}</div></div>
  </div>

  <section>
    <h2>1. General test details</h2>
    <table><tbody>
      <tr><th>Generated</th><td>{_esc(now)}</td></tr>
      <tr><th>Script</th><td>{_esc(script_name)}</td></tr>
      <tr><th>Test duration</th><td>{_esc(_duration_human(duration_ms))} ({_num(duration_ms, 0)} ms)</td></tr>
    </tbody></table>
  </section>

  <section>
    <h2>2. Test observation</h2>
    <ul>{''.join(f'<li>{b}</li>' for b in obs)}</ul>
  </section>

  <section>
    <h2>3. Full transaction table</h2>
    <p class="note">Si.No · TXN name · min · max · avg · count · failed count · perc 50 · perc 90 · perc 95 · perc 99</p>
    <div class="scroll"><table>
      <thead><tr>
        <th class="num">Si.No</th><th>TXN name</th>
        <th class="num">Min</th><th class="num">Max</th><th class="num">Avg</th>
        <th class="num">Count</th><th class="num">Failed count</th>
        <th class="num">Perc 50</th><th class="num">Perc 90</th>
        <th class="num">Perc 95</th><th class="num">Perc 99</th>
      </tr></thead>
      <tbody>{''.join(txn_rows)}</tbody>
    </table></div>
  </section>

  <section>
    <h2>4. Full request table</h2>
    <p class="note">Si.No · TXN · method · URL · min · avg · max · count · failed count · failed %</p>
    <div class="scroll"><table>
      <thead><tr>
        <th class="num">Si.No</th><th>TXN</th><th>Method</th><th>URL</th>
        <th class="num">Min</th><th class="num">Avg</th><th class="num">Max</th>
        <th class="num">Count</th><th class="num">Failed count</th><th class="num">Failed %</th>
      </tr></thead>
      <tbody>{''.join(req_rows)}</tbody>
    </table></div>
  </section>

  <section>
    <h2>5. Failed request list</h2>
    <p class="note">URL and HTTP status as returned (status 0 = connection/network failure).</p>
    <div class="scroll"><table>
      <thead><tr>
        <th class="num">Si.No</th><th>TXN</th><th>Method</th><th>URL</th>
        <th class="num">Status</th><th class="num">Failed</th>
        <th class="num">Total</th><th class="num">Failed %</th>
      </tr></thead>
      <tbody>{''.join(fail_rows_html)}</tbody>
    </table></div>
  </section>

  <section>
    <h2>6. SLA details (thresholds)</h2>
    <div class="scroll"><table>
      <thead><tr><th>Metric</th><th>Threshold</th><th>Observed</th><th>Result</th></tr></thead>
      <tbody>{''.join(sla_rows)}</tbody>
    </table></div>
  </section>

  <footer>Generated by NFE Agent</footer>
</div>
</body>
</html>
"""


def write_html_report(
    *,
    script_path: Union[str, Path],
    points_path: Optional[Union[str, Path]] = None,
    summary_path: Optional[Union[str, Path]] = None,
    html_path: Optional[Union[str, Path]] = None,
) -> str:
    """Build and write ``html-report.html`` beside the script.

    Returns:
        Absolute path to the written HTML file (empty if nothing written).
    """
    script = Path(script_path).resolve()
    points_file = Path(points_path) if points_path else script.with_name("k6-points.json")
    summary_file = Path(summary_path) if summary_path else script.with_name("summary.json")
    out = Path(html_path) if html_path else script.with_name("html-report.html")

    points = load_k6_json_points(points_file)
    summary = None
    if summary_file.is_file():
        try:
            summary = json.loads(summary_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary = None

    html = build_html_report(
        points=points,
        summary=summary,
        script_name=script.name,
    )
    out.write_text(html, encoding="utf-8")
    return str(out.resolve())
