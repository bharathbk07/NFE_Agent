"""Run NFE analyse/k6 for a loaded Watch-me recording (Jira worker path)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.integrations.jira.story_parser import JiraPerfRequest
from src.nodes.analyse import analyse_traffic
from src.utils.artifacts import save_k6_script, save_load_test_ir, stable_artifact_names
from src.utils.k6_generator import emit_k6_from_ir
from src.utils.k6_mcp import run_k6_smoke_preferred
from src.utils.recording_store import load_watch_me_recording, resolve_recording_path

logger = logging.getLogger(__name__)


async def run_perf_for_request(
    request: JiraPerfRequest,
    *,
    recording_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Load recording, analyse, apply workload to IR, emit + smoke k6.

    Returns a result dict suitable for Jira comments (no secrets).
    """
    path = recording_path or resolve_recording_path(
        request.recording_hint or request.host_hint or request.target_url
    )
    if path is None:
        return {
            "ok": False,
            "blocked": True,
            "reason": "recording_missing",
            "recording_hint": request.recording_hint or request.host_hint,
            "target_url": request.target_url,
        }

    loaded = load_watch_me_recording(path)
    target_url = request.target_url or loaded.get("target_url") or ""
    state: Dict[str, Any] = {
        "target_url": target_url,
        "credentials": loaded.get("credentials") or {},
        "user_journey_steps": loaded.get("user_journey_steps") or [],
        "sub_tasks": loaded.get("sub_tasks") or [],
        "run_records": loaded.get("run_records") or [],
        "error_log": [],
        "recording_mode": "reuse",
        "watch_me_status": "ready_analyse",
        "recording_file": str(path),
        "messages": [],
        # Final workload smoke + Confluence publish happen below (avoid double publish).
        "skip_confluence_publish": True,
    }

    analysis = await analyse_traffic(state)
    perf = dict(analysis.get("performance_test_output") or {})
    artifacts = dict(perf.get("artifacts") or {})
    ir = dict(perf.get("load_test_ir") or artifacts.get("load_test_ir") or {})

    if request.workload:
        ir["workload"] = dict(request.workload)
    if request.thresholds:
        ir.setdefault("workload", {})
        ir["workload"]["thresholds"] = dict(request.thresholds)
    ir["target_url"] = ir.get("target_url") or target_url

    names = stable_artifact_names(target_url)
    k6_file: Dict[str, str] = {}
    smoke: Dict[str, Any] = {}
    heal_notes = list((perf.get("k6_smoke") or {}).get("heal_notes") or [])

    k6_script = (
        emit_k6_from_ir(ir)
        if ir.get("transactions") or ir.get("vars") is not None
        else ""
    )
    if not k6_script:
        k6_script = str(artifacts.get("k6_script") or "")

    if k6_script and ir:
        k6_file = save_k6_script(
            k6_script, target_url=target_url, filename=names.get("script")
        )
        ir_meta = save_load_test_ir(
            ir, target_url=target_url, filename=names.get("ir")
        )
        try:
            smoke = await run_k6_smoke_preferred(k6_file.get("path") or "")
        except Exception as exc:
            logger.warning("k6 run failed: %s", exc)
            smoke = {"ok": False, "summary": str(exc)}
    else:
        ir_meta = {}

    prior_k6 = artifacts.get("k6_file") if isinstance(artifacts.get("k6_file"), dict) else {}

    confluence_info: Dict[str, Any] = {"published": False}
    try:
        from src.integrations.confluence import try_publish_run_results

        confluence_info = try_publish_run_results(
            {
                "target_url": target_url,
                "recording_file": str(path),
                "recording_hint": request.recording_hint or "",
                "jira_issue_key": getattr(request, "issue_key", "") or "",
                "k6_path": k6_file.get("path") or prior_k6.get("path") or "",
                "ir_path": ir_meta.get("path") or "",
                "html_report": smoke.get("html_report")
                or (perf.get("k6_smoke") or {}).get("html_report")
                or "",
                "summary_json": str(
                    smoke.get("summary_json")
                    or (perf.get("k6_smoke") or {}).get("summary_json")
                    or ""
                ),
                "smoke_result": smoke or (perf.get("k6_smoke") or {}),
                "smoke_ok": smoke.get("ok") if smoke else (perf.get("k6_smoke") or {}).get("ok"),
                "smoke_summary": str(
                    smoke.get("summary")
                    or (perf.get("k6_smoke") or {}).get("summary")
                    or ""
                ),
                "heal_notes": heal_notes,
                "transactions": analysis.get("transactions")
                or perf.get("transactions")
                or [],
                "failed_checks": list(smoke.get("failed_checks") or []),
                "failed_urls": list(smoke.get("failed_urls") or []),
                "status_counts": dict(smoke.get("status_counts") or {}),
                "exit_code": smoke.get("exit_code"),
            }
        )
    except Exception as exc:
        logger.warning("Confluence publish soft-failed in Jira pipeline: %s", exc)
        confluence_info = {"published": False, "skipped_reason": str(exc)}

    return {
        "ok": bool(smoke.get("ok")) if smoke else False,
        "blocked": False,
        "target_url": target_url,
        "recording_path": str(path),
        "workload": request.workload,
        "transactions": analysis.get("transactions") or perf.get("transactions") or [],
        "k6_path": k6_file.get("path") or prior_k6.get("path") or "",
        "ir_path": ir_meta.get("path") or "",
        "html_report": smoke.get("html_report") or "",
        "smoke_ok": smoke.get("ok") if smoke else None,
        "smoke_summary": str(smoke.get("summary") or ""),
        "heal_notes": heal_notes,
        "skipped": bool(smoke.get("skipped")),
        "failed_checks": list(smoke.get("failed_checks") or []),
        "failed_urls": list(smoke.get("failed_urls") or []),
        "status_counts": dict(smoke.get("status_counts") or {}),
        "summary_json": str(smoke.get("summary_json") or ""),
        "exit_code": smoke.get("exit_code"),
        "confluence": confluence_info,
        "confluence_url": confluence_info.get("run_url") or "",
    }
