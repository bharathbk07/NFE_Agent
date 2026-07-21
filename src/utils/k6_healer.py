"""Deterministic healers for generated k6 scripts after a failed smoke run."""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urlparse

from src.agents.transaction_agent import SPA_CHROME_PATH_HINTS, _is_business_critical_http

logger = logging.getLogger(__name__)


def heal_load_test_ir(
    ir: Dict[str, Any],
    smoke_result: Dict[str, Any],
    *,
    attempt: int = 1,
) -> Tuple[Dict[str, Any], List[str]]:
    """Apply deterministic fixes to IR based on a failed k6 smoke run.

    Args:
        ir: Load-test IR dictionary (mutated copy returned).
        smoke_result: Output of :func:`run_k6_smoke`.
        attempt: Heal attempt number (1-based).

    Returns:
        Pair of (healed IR, human-readable notes describing changes).
    """
    notes: List[str] = []
    healed = dict(ir)
    healed["transactions"] = [dict(t) for t in (ir.get("transactions") or [])]
    healed["correlations"] = list(ir.get("correlations") or [])
    healed["vars"] = list(ir.get("vars") or [])

    if smoke_result.get("skipped") or smoke_result.get("ok"):
        return healed, notes

    # 1) Drop SPA chrome GETs that are still present
    chrome_dropped = 0
    for txn in healed["transactions"]:
        reqs = list(txn.get("requests") or [])
        kept = []
        for r in reqs:
            url = str(r.get("url") or "")
            method = str(r.get("method") or "GET").upper()
            lower = url.lower()
            if method == "GET" and any(h in lower for h in SPA_CHROME_PATH_HINTS):
                chrome_dropped += 1
                continue
            if method == "GET" and not _is_business_critical_http(url, method):
                # On later attempts, drop non-critical GETs that appear in failures
                if attempt >= 2 and _url_mentioned(url, smoke_result):
                    chrome_dropped += 1
                    continue
            kept.append(r)
        txn["requests"] = kept
    if chrome_dropped:
        notes.append(f"Removed {chrome_dropped} non-critical/chrome GET request(s).")

    # 2) Soften checks ONLY on non-critical GETs (never soft 401-prone APIs)
    soft = 0
    for txn in healed["transactions"]:
        for r in txn.get("requests") or []:
            method = str(r.get("method") or "GET").upper()
            if method != "GET":
                continue
            url = str(r.get("url") or "")
            if _is_business_critical_http(url, method):
                # Critical GETs must stay hard — soft-pass hides 401/404 product failure
                if r.get("soft_check"):
                    r["soft_check"] = False
                continue
            if attempt >= 2:
                if not r.get("soft_check"):
                    r["soft_check"] = True
                    soft += 1
                continue
            if not r.get("soft_check"):
                r["soft_check"] = True
                soft += 1
    if soft:
        notes.append(f"Relaxed status checks on {soft} non-critical GET request(s).")

    # 3) Dedupe correlation vars that collide on the same name
    before = len(healed["correlations"])
    healed["correlations"] = _dedupe_correlations(healed["correlations"])
    after = len(healed["correlations"])
    if after < before:
        notes.append(f"Deduped {before - after} duplicate correlation(s).")

    # 4) Prefer alternate JSON extract paths when vars stay empty (4xx later)
    path_fixes = _fix_extract_paths(healed, smoke_result)
    if path_fixes:
        notes.extend(path_fixes)

    # 5) Real product fixes (not soft-pass): auth + create-id extract + typeahead
    from src.utils.load_test_ir import (
        _coalesce_typeahead_requests,
        _ensure_auth_csrf,
        _inject_missing_auth,
        _retarget_create_id_extracts,
    )

    before_corr = json_dumps_safe(healed.get("correlations"))
    healed["correlations"] = _retarget_create_id_extracts(healed.get("correlations") or [])
    if json_dumps_safe(healed.get("correlations")) != before_corr:
        notes.append("Retargeted create-resource ID extracts to the create POST response.")

    for txn in healed["transactions"]:
        before_n = len(txn.get("requests") or [])
        txn["requests"] = _coalesce_typeahead_requests(list(txn.get("requests") or []))
        after_n = len(txn.get("requests") or [])
        if after_n < before_n:
            notes.append(
                f"Dropped {before_n - after_n} typeahead intermediate GET(s) in `{txn.get('name')}`."
            )

    before_auth = _has_auth_post_local(healed["transactions"])
    healed["transactions"] = _inject_missing_auth(
        healed["transactions"],
        origin=str(healed.get("origin") or ""),
        vars_list=list(healed.get("vars") or []),
        target_url=str(healed.get("target_url") or ""),
        correlations=healed["correlations"],
    )
    _ensure_auth_csrf(
        healed["transactions"],
        healed["correlations"],
        origin=str(healed.get("origin") or ""),
    )
    if not before_auth and _has_auth_post_local(healed["transactions"]):
        notes.append("Injected missing login POST + CSRF token extract.")
    elif any(str(c.get("var")) == "csrf_token" for c in healed["correlations"]):
        notes.append("Ensured login CSRF `csrf_token` extract → auth/validate.")

    if not notes:
        notes.append(
            "No deterministic heal applied — review failed checks and correlations."
        )
    healed["heal_notes"] = list(healed.get("heal_notes") or []) + notes
    healed["heal_attempt"] = attempt
    return healed, notes


def json_dumps_safe(obj: Any) -> str:
    """Stable-ish string compare helper for heal notes."""
    import json

    try:
        return json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        return str(obj)


def _has_auth_post_local(transactions: List[Dict[str, Any]]) -> bool:
    for txn in transactions or []:
        for r in txn.get("requests") or []:
            method = str(r.get("method") or "").upper()
            url = str(r.get("url") or "").lower()
            if method == "POST" and "/auth/" in url:
                return True
    return False


def _url_mentioned(url: str, smoke_result: Dict[str, Any]) -> bool:
    """Return True if a request URL appears in smoke failure output."""
    needle = urlparse(url).path.rstrip("/")
    if not needle or needle == "/":
        return False
    blob = " ".join(
        [
            str(smoke_result.get("stdout") or ""),
            str(smoke_result.get("stderr") or ""),
            " ".join(smoke_result.get("failed_urls") or []),
            " ".join(smoke_result.get("failed_checks") or []),
        ]
    )
    return needle in blob


def _dedupe_correlations(correlations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep one correlation per var name, preferring JSON body extracts."""
    best: Dict[str, Dict[str, Any]] = {}

    def score(c: Dict[str, Any]) -> int:
        loc = str((c.get("extract") or {}).get("from_location") or "")
        s = 0
        if "referenceId" in loc or "empNumber" in loc:
            s += 20
        if loc.startswith("body.$"):
            s += 10
        if "subunit" in loc or loc.endswith(".raw"):
            s -= 30
        if c.get("confidence") == "high":
            s += 5
        return s

    for c in correlations:
        var = str(c.get("var") or "").lower()
        if not var:
            continue
        prev = best.get(var)
        if prev is None or score(c) > score(prev):
            best[var] = c
    return list(best.values())


def _fix_extract_paths(
    ir: Dict[str, Any], smoke_result: Dict[str, Any]
) -> List[str]:
    """Swap weak extract locations for common OrangeHRM-style paths."""
    notes: List[str] = []
    alts = {
        "referenceid": ["body.$.data.referenceId", "body.$.data.data.referenceId"],
        "empnumber": ["body.$.data[0].empNumber", "body.$.data.empNumber"],
        "requestid": ["body.$.data.id", "body.$.data.requestId"],
        "id": ["body.$.data.id"],
    }
    for c in ir.get("correlations") or []:
        var = str(c.get("var") or "").lower()
        ex = c.get("extract") or {}
        loc = str(ex.get("from_location") or "")
        if var not in alts:
            continue
        if loc.startswith("body.$") and "raw" not in loc and "[" not in loc.replace(
            "data[0]", ""
        ):
            # Already a clean path
            if "referenceId" in loc or "empNumber" in loc or loc.endswith(".id"):
                continue
        for candidate in alts[var]:
            if candidate != loc:
                ex = dict(ex)
                ex["from_location"] = candidate
                c["extract"] = ex
                notes.append(f"Retargeted extract for `{c.get('var')}` → `{candidate}`.")
                break
    return notes


def format_smoke_section(smoke: Dict[str, Any], heal_notes: List[str]) -> str:
    """Markdown for playbook/report about smoke validation."""
    lines = ["### Smoke validation (k6)", ""]
    via = smoke.get("via") or smoke.get("validated_via") or "cli"
    if smoke.get("skipped"):
        lines.append(
            f"_Smoke not run:_ {smoke.get('stderr') or smoke.get('summary') or 'k6 unavailable'}."
        )
        lines.append("")
        return "\n".join(lines)

    if smoke.get("ok"):
        lines.append(
            f"- **Result:** passed (`{smoke.get('summary')}`) — 1 VU × 2 iterations "
            f"(via `{via}`)."
        )
    else:
        lines.append(
            f"- **Result:** failed (`{smoke.get('summary')}`, via `{via}`)."
        )
        fails = smoke.get("failed_checks") or []
        if fails:
            lines.append("- **Failed checks:** " + ", ".join(f"`{f}`" for f in fails[:8]))
    html_report = smoke.get("html_report") or ""
    if html_report:
        lines.append(
            f"- **HTML report:** `{html_report}` "
            "(general details, observations, TXN summary, failures, SLA)."
        )
    if heal_notes:
        lines.append("- **Heal notes:**")
        for n in heal_notes[:8]:
            lines.append(f"  - {n}")
    lines.append("")
    return "\n".join(lines)
