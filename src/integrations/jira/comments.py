"""Jira comment templates (lightweight markup → ADF via client.add_comment).

Jira Cloud REST v3 expects Atlassian Document Format for comment bodies.
Templates use a small markup (``##`` headings, ``*`` bullets, `` `code` ``)
that ``report_markup_to_adf`` converts before posting.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.integrations.jira.security import sanitize_comment


def comment_queued(issue_key: str) -> str:
    return sanitize_comment(
        f"*NFE Agent* queued `{issue_key}`.\n\n"
        "Parsing description / acceptance criteria and checking for a Watch-me recording…"
    )


def comment_missing_recording(
    *,
    target_url: str = "",
    recording_hint: str = "",
    host_hint: str = "",
) -> str:
    hint = recording_hint or host_hint or "(derive from target_url)"
    lines = [
        "*NFE Agent* — recording not found",
        "",
        "## Next steps",
    ]
    if target_url:
        lines.append(f"* Target URL: `{target_url}`")
    lines.extend(
        [
            f"* Expected recording key/host: `{hint}`",
            "* Run Watch-me locally (LangGraph Studio) for this journey.",
            "* Confirm `artifacts/recordings/<name>.json` exists.",
            "* Add label `nfe-recording-ready` on this issue to resume.",
        ]
    )
    return sanitize_comment("\n".join(lines))


def comment_blocked(reason: str) -> str:
    return sanitize_comment(
        f"*NFE Agent* blocked\n\n## Reason\n\n{reason}"
    )


def _load_summary_data(summary_json_path: str) -> Dict[str, Any]:
    path = (summary_json_path or "").strip()
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_summary_metrics(summary_json_path: str) -> Dict[str, Any]:
    data = _load_summary_data(summary_json_path)
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


def _threshold_rows(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
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


def _why_failed_section(
    *,
    smoke_ok: Optional[bool],
    smoke_summary: str,
    skipped: bool,
    exit_code: Optional[int],
    failed_checks: List[str],
    failed_urls: List[str],
    status_counts: Dict[str, Any],
    summary_json: str,
) -> List[str]:
    """Build a leading 'Why it failed / stopped' section when not a clean pass."""
    if smoke_ok:
        return []

    summary = _load_summary_data(summary_json)
    thr = _threshold_rows(summary)
    thr_fails = [r for r in thr if not r["ok"]]
    summary_l = (smoke_summary or "").lower()
    lines = ["## Why it failed / stopped", ""]

    if skipped or "k6 missing" in summary_l or "k6 not found" in summary_l:
        lines.append(
            "* Run did **not** complete: k6 was skipped or unavailable "
            f"(`{smoke_summary or 'skipped'}`)."
        )
    elif exit_code == -1 or any(
        t in summary_l for t in ("timeout", "timed out", "spawn failed", "script missing")
    ):
        lines.append(
            f"* Run **stopped mid-way** / did not finish "
            f"(`{smoke_summary or exit_code}`)."
        )
    elif thr_fails:
        lines.append("* Test **completed**, but **SLA / thresholds failed**:")
        for r in thr_fails[:15]:
            lines.append(
                f"  * `{r['metric']}` → `{r['threshold']}`"
            )
        if failed_urls or failed_checks:
            lines.append("* Additional script/check issues:")
    elif smoke_ok is None:
        lines.append("* Smoke status unknown (no conclusive pass/fail).")
    else:
        lines.append(
            "* Test **completed** (or attempted), but smoke checks / script validation failed."
        )

    if failed_urls:
        lines.append("* Top failed requests:")
        for u in failed_urls[:10]:
            lines.append(f"  * `{u}`")
    if failed_checks:
        lines.append("* Failed checks:")
        for c in failed_checks[:10]:
            lines.append(f"  * `{c}`")
    if status_counts:
        status_line = ", ".join(
            f"{code}×{count}" for code, count in sorted(status_counts.items())
        )
        lines.append(f"* HTTP status counts: `{status_line}`")
    if exit_code is not None:
        lines.append(f"* Exit code: `{exit_code}`")
    if smoke_summary and smoke_ok is False:
        lines.append(f"* Run notes: {smoke_summary}")

    lines.append("")
    return lines


def comment_results(
    *,
    issue_key: str,
    target_url: str,
    smoke_ok: Optional[bool],
    smoke_summary: str = "",
    workload: Optional[Dict[str, Any]] = None,
    k6_path: str = "",
    ir_path: str = "",
    html_report: str = "",
    transactions: Optional[List[Dict[str, Any]]] = None,
    heal_notes: Optional[List[str]] = None,
    story_summary: str = "",
    recording_path: str = "",
    failed_checks: Optional[List[str]] = None,
    failed_urls: Optional[List[str]] = None,
    status_counts: Optional[Dict[str, Any]] = None,
    summary_json: str = "",
    exit_code: Optional[int] = None,
    skipped: bool = False,
    confluence_url: str = "",
    extra: str = "",
) -> str:
    """Rich test report comment (converted to ADF when posted)."""
    status = "PASSED" if smoke_ok else ("SKIPPED" if smoke_ok is None else "FAILED")
    wl = workload or {}
    if wl:
        wl_text = ", ".join(f"{k}={v}" for k, v in list(wl.items())[:8])
    else:
        wl_text = "default smoke (1 VU × 2 iterations)"

    metrics = _load_summary_metrics(summary_json)
    status_counts = status_counts or {}
    status_line = (
        ", ".join(f"{code}×{count}" for code, count in sorted(status_counts.items()))
        if status_counts
        else "n/a"
    )

    txn_lines = [
        f"* `{t.get('name') or t.get('id') or '?'}`"
        for t in (transactions or [])[:40]
    ]
    fail_url_lines = [f"* `{u}`" for u in (failed_urls or [])[:25]]
    fail_check_lines = [f"* `{c}`" for c in (failed_checks or [])[:25]]
    heal_lines = [f"* {n}" for n in (heal_notes or [])[:15]]

    why = _why_failed_section(
        smoke_ok=smoke_ok,
        smoke_summary=smoke_summary,
        skipped=skipped,
        exit_code=exit_code,
        failed_checks=list(failed_checks or []),
        failed_urls=list(failed_urls or []),
        status_counts=status_counts,
        summary_json=summary_json,
    )

    parts = [
        f"*NFE Agent* — Test Report for `{issue_key}`",
        "",
        *why,
        "## Test Summary",
        f"* Status: *{status}*",
        f"* Story: {story_summary or 'n/a'}",
        f"* Target: `{target_url or 'n/a'}`",
        f"* Workload: `{wl_text}`",
        f"* Recording: `{recording_path or 'n/a'}`",
        f"* Run notes: {smoke_summary or 'None'}",
        "",
        "## Statistics",
        f"* HTTP requests: `{_fmt_num(metrics.get('http_reqs'), digits=0)}`",
        f"* HTTP fail rate: `{_fmt_pct(metrics.get('fail_rate'))}`",
        (
            f"* Duration p95 / avg / max: "
            f"`{_fmt_ms(metrics.get('p95_ms'))}` / "
            f"`{_fmt_ms(metrics.get('avg_ms'))}` / "
            f"`{_fmt_ms(metrics.get('max_ms'))}`"
        ),
        f"* Iterations: `{_fmt_num(metrics.get('iterations'), digits=0)}`",
        (
            f"* Checks pass / fail: "
            f"`{_fmt_num(metrics.get('checks_passes'), digits=0)}` / "
            f"`{_fmt_num(metrics.get('checks_fails'), digits=0)}`"
        ),
        f"* Test duration: `{_fmt_ms(metrics.get('duration_ms'))}`",
        f"* Exit code: `{exit_code if exit_code is not None else 'n/a'}`",
        f"* HTTP status counts: `{status_line}`",
        "",
        "## Failed Requests",
        ("\n".join(fail_url_lines) if fail_url_lines else "None"),
        "",
        "## Failed Checks",
        ("\n".join(fail_check_lines) if fail_check_lines else "None"),
        "",
        "## Transactions",
        ("\n".join(txn_lines) if txn_lines else "None"),
        "",
        "## Artifacts",
        f"* k6 script: `{k6_path or 'n/a'}`",
        f"* IR: `{ir_path or 'n/a'}`",
        f"* HTML report: `{html_report or 'n/a'}`",
        f"* Summary JSON: `{summary_json or 'n/a'}`",
        "",
        "## Heal notes",
        ("\n".join(heal_lines) if heal_lines else "None"),
    ]
    if confluence_url:
        parts.extend(
            [
                "",
                "## Confluence",
                f"* Run report: {confluence_url}",
            ]
        )
    if extra:
        parts.extend(["", extra])
    return sanitize_comment("\n".join(parts))
