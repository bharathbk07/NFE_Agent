"""Pre-smoke assertion coverage gate for Load-Test IR / k6 scripts."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.utils.k6_generator import emit_k6_from_ir
from src.utils.load_test_ir import ensure_txn_assertions, validate_txn_assertions


def prepare_ir_and_script_for_smoke(
    ir: Dict[str, Any],
    script: str,
    *,
    network_requests: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], str, bool, List[str]]:
    """Ensure assertion coverage, re-emit if needed, and report pass/fail.

    Flow:
      1. If IR coverage incomplete → ``apply_txn_anchor_assertions`` + re-emit
      2. Re-validate IR + emitted script
      3. Return ``ok=False`` when still short (caller must **not** run k6)

    Returns:
        ``(ir, script, ok, notes)``
    """
    notes: List[str] = []
    ir, fix_notes = ensure_txn_assertions(ir, network_requests=network_requests)
    if fix_notes:
        notes.append(fix_notes[0])  # "Re-applied..."
        script = emit_k6_from_ir(ir)

    ok, val_notes = validate_txn_assertions(ir, script=script)
    if not ok:
        notes.extend(val_notes)
    elif fix_notes:
        notes.append("Assertion coverage OK after re-applying anchors.")
    return ir, script, ok, notes


def assertion_coverage_failure_result(notes: List[str]) -> Dict[str, Any]:
    """Build a smoke-shaped result when assertion coverage blocks the run."""
    summary = "assertion coverage failed — fix script before running k6"
    if notes:
        summary = summary + ": " + "; ".join(notes[:6])
    return {
        "ok": False,
        "skipped": False,
        "summary": summary,
        "exit_code": -1,
        "failed_checks": list(notes)[:40],
        "failed_urls": [],
        "status_counts": {},
        "assertion_gate_failed": True,
    }
