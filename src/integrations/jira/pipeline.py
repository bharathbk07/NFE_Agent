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
    from src.utils.app_registry import resolve_app_and_flow

    app_id, flow_id = resolve_app_and_flow(
        target_url=target_url,
        label=loaded.get("recording_label") or loaded.get("flow") or "",
        recording_hint=request.recording_hint or "",
        explicit_app=loaded.get("app") or "",
    )
    # Prefer story credentials (per-app), then recording-stored credentials.
    merged_creds = dict(loaded.get("credentials") or {})
    if request.credentials:
        merged_creds.update(request.credentials)
    state: Dict[str, Any] = {
        "target_url": target_url,
        "credentials": merged_creds,
        "user_journey_steps": loaded.get("user_journey_steps") or [],
        "sub_tasks": loaded.get("sub_tasks") or [],
        "run_records": loaded.get("run_records") or [],
        "error_log": [],
        "recording_mode": "reuse",
        "watch_me_status": "ready_analyse",
        "recording_file": str(path),
        "app": app_id,
        "flow": flow_id,
        "recording_label": loaded.get("recording_label") or flow_id,
        "messages": [],
        # Final workload smoke + Confluence publish happen below (avoid double publish
        # and avoid analyse's default 1 VU × 2 smoke when the story has a workload).
        "skip_confluence_publish": True,
        "skip_k6_smoke": True,
    }

    analysis = await analyse_traffic(state)
    perf = dict(analysis.get("performance_test_output") or {})
    artifacts = dict(perf.get("artifacts") or {})
    ir = dict(perf.get("load_test_ir") or artifacts.get("load_test_ir") or {})

    story_workload = dict(request.workload or {})
    if story_workload:
        ir["workload"] = dict(story_workload)
        workload_source = "jira_story"
    else:
        workload_source = "default_smoke"
    if request.thresholds:
        ir.setdefault("workload", {})
        ir["workload"]["thresholds"] = dict(request.thresholds)
        if not story_workload:
            # Thresholds alone still come from the story
            workload_source = "jira_story"
    ir["target_url"] = ir.get("target_url") or target_url

    names = stable_artifact_names(target_url, app=app_id, flow=flow_id)
    app_id = names.get("app") or app_id
    flow_id = names.get("flow") or flow_id
    k6_file: Dict[str, str] = {}
    smoke: Dict[str, Any] = {}
    heal_notes = list((perf.get("k6_smoke") or {}).get("heal_notes") or [])
    ir_meta: Dict[str, str] = {}

    can_emit = bool(ir.get("transactions") or ir.get("vars") is not None)
    k6_script = emit_k6_from_ir(ir) if can_emit else ""
    if not k6_script and not story_workload:
        # Only fall back to analyse script when there is no story workload
        # (otherwise we must not re-run the default smoke options).
        k6_script = str(artifacts.get("k6_script") or "")

    wl_for_log = ir.get("workload") or {}
    logger.info(
        "Running k6 with %s workload: vus=%s executor=%s iterations=%s duration=%s",
        workload_source,
        wl_for_log.get("vus"),
        wl_for_log.get("executor"),
        wl_for_log.get("iterations"),
        wl_for_log.get("duration") or wl_for_log.get("maxDuration"),
    )

    if k6_script and ir:
        from src.utils.k6_assertion_gate import (
            assertion_coverage_failure_result,
            prepare_ir_and_script_for_smoke,
        )

        network_reqs: list = []
        for rec in loaded.get("run_records") or []:
            if isinstance(rec, dict) and rec.get("network_requests"):
                network_reqs = list(rec.get("network_requests") or [])
                break

        ir, k6_script, assert_ok, assert_notes = prepare_ir_and_script_for_smoke(
            ir,
            k6_script,
            network_requests=network_reqs,
        )
        heal_notes.extend(assert_notes)

        k6_file = save_k6_script(
            k6_script,
            target_url=target_url,
            filename=names.get("script"),
            app=app_id,
            flow=flow_id,
        )
        ir_meta = save_load_test_ir(
            ir,
            target_url=target_url,
            filename=names.get("ir"),
            app=app_id,
            flow=flow_id,
        )
        if not assert_ok:
            logger.warning(
                "Assertion coverage gate blocked Jira k6 run: %s",
                "; ".join(assert_notes[:5]),
            )
            smoke = assertion_coverage_failure_result(assert_notes)
        else:
            try:
                smoke = await run_k6_smoke_preferred(k6_file.get("path") or "")
            except Exception as exc:
                logger.warning("k6 run failed: %s", exc)
                smoke = {"ok": False, "summary": str(exc)}

        # Heal loop — same pattern as analyse_traffic; runs against real workload
        max_heals = 2
        attempt = 0
        while (
            smoke.get("ok") is False
            and not smoke.get("skipped")
            and not smoke.get("assertion_gate_failed")
            and attempt < max_heals
        ):
            attempt += 1
            from src.utils.k6_generator import emit_k6_from_ir as _emit
            from src.utils.k6_healer import heal_load_test_ir

            ir, heal_attempt_notes = heal_load_test_ir(
                ir, smoke, attempt=attempt
            )
            heal_notes.extend(heal_attempt_notes)
            # Preserve story workload in the healed IR
            if story_workload:
                ir["workload"] = dict(story_workload)
            k6_script = _emit(ir)
            ir, k6_script, assert_ok, assert_notes = prepare_ir_and_script_for_smoke(
                ir,
                k6_script,
                network_requests=network_reqs,
            )
            heal_notes.extend(assert_notes)
            k6_file = save_k6_script(
                k6_script,
                target_url=target_url,
                filename=names.get("script"),
                app=app_id,
                flow=flow_id,
            )
            ir_meta = save_load_test_ir(
                ir,
                target_url=target_url,
                filename=names.get("ir"),
                app=app_id,
                flow=flow_id,
            )
            if not assert_ok:
                logger.warning(
                    "Assertion coverage gate blocked Jira k6 after heal %s: %s",
                    attempt,
                    "; ".join(assert_notes[:5]),
                )
                smoke = assertion_coverage_failure_result(assert_notes)
                break
            try:
                smoke = await run_k6_smoke_preferred(k6_file.get("path") or "")
            except Exception as exc:
                logger.warning("k6 run failed after heal %s: %s", attempt, exc)
                smoke = {"ok": False, "summary": str(exc)}
            if smoke.get("ok"):
                heal_notes.append(f"Smoke passed after heal attempt {attempt}.")
                break
    else:
        if story_workload and not k6_script:
            logger.error(
                "Story workload present but k6 emit produced empty script "
                "(transactions=%s)",
                bool(ir.get("transactions")),
            )
            smoke = {
                "ok": False,
                "summary": "k6 emit failed with story workload",
                "exit_code": -1,
            }

    # Refresh knowledge after authoritative Jira workload smoke
    try:
        if app_id and (k6_file or smoke):
            from src.utils.knowledge_store import upsert_flow_card

            smoke_ok = smoke.get("ok")
            if smoke.get("skipped"):
                smoke_status = "skipped"
            elif smoke_ok is True:
                smoke_status = "passed"
            elif smoke_ok is False:
                smoke_status = "failed"
            else:
                smoke_status = "unknown"
            txns = analysis.get("transactions") or perf.get("transactions") or []
            txn_names = [
                str(t.get("name") or t.get("id") or "")
                for t in txns
                if isinstance(t, dict)
            ]
            upsert_flow_card(
                app_id,
                flow_id or "default",
                target_url=target_url,
                recording_path=str(path),
                k6_path=k6_file.get("path") or "",
                ir_path=ir_meta.get("path") or "",
                txn_names=[n for n in txn_names if n],
                workload_source=workload_source,
                smoke_status=smoke_status,
                step_count=len(loaded.get("user_journey_steps") or []),
            )
            try:
                from src.utils.knowledge_store import ingest_run_history

                ingest_run_history(
                    app_id,
                    flow_id or "default",
                    smoke=smoke or {},
                    workload_source=workload_source,
                    k6_path=k6_file.get("path") or "",
                    summary_json=str(smoke.get("summary_json") or ""),
                    target_url=target_url,
                )
            except Exception as run_err:
                logger.warning("Run history ingest skipped (Jira): %s", run_err)
    except Exception as know_err:
        logger.warning("Knowledge upsert skipped (Jira): %s", know_err)

    prior_k6 = artifacts.get("k6_file") if isinstance(artifacts.get("k6_file"), dict) else {}
    aborted_by_watcher = bool(smoke.get("aborted_by_watcher"))

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
                "workload": ir.get("workload") or story_workload or {},
                "workload_source": workload_source,
                "aborted_by_watcher": aborted_by_watcher,
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
        "workload": ir.get("workload") or story_workload or {},
        "workload_source": workload_source,
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
        "aborted_by_watcher": aborted_by_watcher,
        "confluence": confluence_info,
        "confluence_url": confluence_info.get("run_url") or "",
        "confluence_skipped_reason": confluence_info.get("skipped_reason") or "",
    }
