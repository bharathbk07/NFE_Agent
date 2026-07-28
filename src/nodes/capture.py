"""Browser capture, Watch-me, replay, and navigator automation nodes."""

import logging
import re
from typing import Any, Dict, List, Literal

from langchain_core.messages import AIMessage

from src.agents.intent_router import get_latest_human_text
from src.agents.navigator_agent import NavigatorAgent
from src.agents.state import AgentState
from src.exceptions import (
    ErrorCode,
    NFEError,
    NFEPipelineError,
    NFESecurityError,
    NFEValidationError,
    node_failure_update,
    to_error_log_entry,
    to_user_message,
    wrap_unexpected,
)
from src.tools.playwright_tool import PlaywrightBrowserRecorder

logger = logging.getLogger("AgentGraph")


async def plan_navigator_steps(state: AgentState) -> Dict[str, Any]:
    """Plan and merge Playwright steps for all journey subtasks.

    Args:
        state: State containing the target, credentials, and subtasks.

    Returns:
        A partial state whose ``user_journey_steps`` value is a list of step
        dictionaries, or an empty update when prior errors exist.

    Raises:
        Exception: If navigator planning fails.
    """
    logger.info("Node: plan_navigator_steps starting...")

    if state.get("error_log"):
        return {}

    url = state["target_url"]
    credentials = state.get("credentials", {})
    sub_tasks = state.get("sub_tasks", [])
    raw_steps = state.get("user_journey_steps", [])

    is_already_planned = (
        isinstance(raw_steps, list)
        and len(raw_steps) > 0
        and all(isinstance(s, dict) and "action" in s for s in raw_steps)
    )

    if is_already_planned:
        return {"user_journey_steps": raw_steps}

    navigator = NavigatorAgent()
    all_steps: List[Dict[str, Any]] = []
    seen_navigate = False

    for task in sub_tasks:
        logger.info(f"Sub-agent planning steps for: {task.get('name', 'unknown')}")
        task_steps = await navigator.aplan_steps(url, credentials, task["description"])
        for step in task_steps:
            if step.get("action") == "navigate" and seen_navigate:
                continue
            if step.get("action") == "navigate":
                seen_navigate = True
            all_steps.append({**step, "sub_task": task.get("name", "main_flow")})

    if not all_steps and sub_tasks:
        all_steps = await navigator.aplan_steps(
            url, credentials, sub_tasks[0]["description"]
        )

    return {"user_journey_steps": all_steps}


async def watch_me_record(state: AgentState) -> Dict[str, Any]:
    """Open a headed browser and record the user's interactive journey as Run 1.

    Args:
        state: State containing ``target_url`` from orchestration.

    Returns:
        Recorded Playwright steps, Run 1 capture data, and chat status messages.
    """
    import asyncio

    logger.info("Node: watch_me_record starting...")

    if state.get("error_log"):
        return {}

    url = state.get("target_url") or ""
    if not url:
        return {
            "error_log": ["Watch-me recording requires target_url."],
            "watch_me_status": "missing_url",
            "messages": [
                AIMessage(content="Watch-me needs a target URL before opening the browser.")
            ],
        }

    recorder = PlaywrightBrowserRecorder(debug_mode=True)
    from src.utils.model_router import allow_blocking_io

    def _record():
        """Run headed Watch-me capture in a worker thread."""
        with allow_blocking_io():
            return recorder.record_watch_me(url)

    error_log = list(state.get("error_log") or [])
    try:
        result = await asyncio.to_thread(_record)
    except NFESecurityError:
        raise
    except NFEError as e:
        return {
            **node_failure_update(
                e,
                prior_error_log=error_log,
                logger=logger,
                context="watch_me_record",
                extra={
                    "watch_me_status": "failed",
                    "messages": [
                        AIMessage(
                            content=(
                                f"{to_user_message(e)}\n\n"
                                "Use local `langgraph dev --allow-blocking` on a machine with a display."
                            )
                        )
                    ],
                },
            )
        }
    except Exception as e:
        wrapped = wrap_unexpected(
            e,
            code=ErrorCode.PIPELINE,
            user_message="Watch-me recording failed.",
        )
        return {
            **node_failure_update(
                wrapped,
                prior_error_log=error_log,
                logger=logger,
                context="watch_me_record",
                extra={
                    "watch_me_status": "failed",
                    "messages": [
                        AIMessage(
                            content=(
                                f"{to_user_message(wrapped)}\n\n"
                                "Use local `langgraph dev --allow-blocking` on a machine with a display."
                            )
                        )
                    ],
                },
            )
        }

    recorded_steps = result.get("recorded_steps") or []
    if result.get("cancelled"):
        return {
            "user_journey_steps": [],
            "run_records": [],
            "error_log": ["Watch-me recording cancelled by user"],
            "recording_mode": "watch_me",
            "watch_me_status": "cancelled",
            "messages": [
                AIMessage(
                    content=(
                        "Watch-me recording **cancelled**. "
                        "No replay or analysis was run. Say **watch me** again when ready."
                    )
                )
            ],
        }

    if result.get("error"):
        error_log.append(result["error"])
        return {
            "user_journey_steps": recorded_steps,
            "run_records": [{
                "run_id": 1,
                "network_requests": result.get("network_requests") or [],
                "step_timeline": result.get("step_timeline") or [],
                "cookies": result.get("cookies") or [],
                "local_storage": result.get("local_storage") or {},
                "session_storage": result.get("session_storage") or {},
                "screenshot_paths": [],
            }] if result.get("network_requests") is not None else [],
            "error_log": error_log,
            "recording_mode": "watch_me",
            "watch_me_status": "timed_out" if "timed out" in str(result.get("error")).lower() else "failed",
            "messages": [
                AIMessage(
                    content=(
                        f"Watch-me stopped early: {result['error']}\n"
                        f"Captured **{len(recorded_steps)}** step(s) before stop."
                    )
                )
            ],
        }

    run_records = [{
        "run_id": 1,
        "network_requests": result.get("network_requests") or [],
        "step_timeline": result.get("step_timeline") or [],
        "cookies": result.get("cookies") or [],
        "local_storage": result.get("local_storage") or {},
        "session_storage": result.get("session_storage") or {},
        "screenshot_paths": [],
    }]

    recording_meta: Dict[str, str] = {}
    try:
        from src.utils.recording_store import save_watch_me_recording
        from src.utils.app_registry import resolve_app_and_flow

        label = state.get("recording_label") or state.get("flow") or ""
        app, flow = resolve_app_and_flow(
            target_url=url,
            label=label,
            explicit_app=state.get("app") or "",
        )
        recording_meta = save_watch_me_recording(
            target_url=url,
            user_journey_steps=recorded_steps,
            run_records=run_records,
            credentials=state.get("credentials") or {},
            sub_tasks=state.get("sub_tasks") or [],
            label=label,
            app=app,
            flow=flow,
        )
    except Exception as save_err:
        logger.warning("Failed to save Watch-me recording: %s", save_err)

    saved_note = ""
    if recording_meta.get("relative_path"):
        saved_note = (
            f" Saved to `{recording_meta['relative_path']}` "
            "(reuse later with **analyse saved recording**)."
        )

    out: Dict[str, Any] = {
        "user_journey_steps": recorded_steps,
        "run_records": run_records,
        "recording_mode": "watch_me",
        "watch_me_status": "recorded",
        "recording_file": recording_meta.get("path") or "",
        "error_log": [],
        "messages": [
            AIMessage(
                content=(
                    f"Recording finished — **{len(recorded_steps)}** step(s) captured."
                    f"{saved_note} "
                    "Replaying headless for correlation (Run 2)…"
                )
            )
        ],
    }
    if recording_meta.get("app"):
        out["app"] = recording_meta["app"]
    if recording_meta.get("flow"):
        out["flow"] = recording_meta["flow"]
    return out


async def replay_recorded_journey(state: AgentState) -> Dict[str, Any]:
    """Replay Watch-me steps headless as Run 2 (no navigator planning).

    Harvests state-mutating HTTP payload literals from Run 1, then rewrites
    matching egress on Run 2 via protocol-level ``page.route`` randomization
    so unique-constraint collisions do not pollute differential correlation.

    Args:
        state: State with ``user_journey_steps`` and Run 1 already populated.

    Returns:
        Updated ``run_records`` including Run 2, or errors if replay fails.
        Also returns ``randomization_ledger`` / ``randomization_state`` for the
        Correlation Engine.
    """
    import asyncio

    logger.info("Node: replay_recorded_journey starting...")

    if state.get("error_log") and not state.get("run_records"):
        return {}

    url = state.get("target_url") or ""
    steps = state.get("user_journey_steps") or []
    run_records = list(state.get("run_records") or [])
    error_log = list(state.get("error_log") or [])

    if not steps:
        error_log.append("No recorded steps to replay.")
        return {
            "error_log": error_log,
            "watch_me_status": "no_steps",
            "messages": [
                AIMessage(
                    content=(
                        "No interaction steps were recorded. "
                        "Try Watch-me again and click through the flow before **Done recording**."
                    )
                )
            ],
        }

    from src.utils.data_randomization import build_middleware_from_run1

    randomization = None
    if run_records:
        randomization = build_middleware_from_run1(
            run_records[0].get("network_requests") or []
        )
        logger.info(
            "Run 1 payload harvest: %s transform(s), %s non-randomizable route(s)",
            len(randomization.transforms),
            len(randomization.non_randomizable_routes()),
        )

    recorder = PlaywrightBrowserRecorder(debug_mode=False)
    from src.utils.model_router import allow_blocking_io

    def _execute():
        """Headless replay of recorded steps with HTTP payload randomization."""
        with allow_blocking_io():
            return recorder.execute_journey(
                url, steps, clear_context=True, randomization=randomization
            )

    try:
        logger.info("Watch-me replay RUN 2 (%s steps)...", len(steps))
        run2_data = await asyncio.to_thread(_execute)
        run_records.append({
            "run_id": 2,
            "network_requests": run2_data.get("network_requests") or [],
            "step_timeline": run2_data.get("step_timeline") or [],
            "cookies": run2_data.get("cookies") or [],
            "local_storage": run2_data.get("local_storage") or {},
            "session_storage": run2_data.get("session_storage") or {},
            "screenshot_paths": [],
        })
        if run2_data.get("error"):
            error_log.append(f"Run 2 (replay) incomplete: {run2_data['error']}")
        if run2_data.get("randomization_state"):
            from src.utils.data_randomization import DataRandomizationMiddleware

            randomization = DataRandomizationMiddleware.from_dict(
                run2_data["randomization_state"]
            )
    except NFESecurityError:
        raise
    except NFEError as e:
        from src.exceptions import log_exception

        log_exception(logger, e, context="watch_me_replay")
        error_log.append(to_error_log_entry(e))
    except Exception as e:
        wrapped = wrap_unexpected(e, user_message="Watch-me replay (Run 2) failed.")
        from src.exceptions import log_exception

        log_exception(logger, wrapped, context="watch_me_replay")
        error_log.append(to_error_log_entry(wrapped))

    # Refresh on-disk recording with Run 1 + Run 2 for full reuse.
    try:
        from src.utils.recording_store import save_watch_me_recording
        from src.utils.app_registry import resolve_app_and_flow

        label = state.get("recording_label") or state.get("flow") or ""
        app, flow = resolve_app_and_flow(
            target_url=url,
            label=label,
            explicit_app=state.get("app") or "",
        )
        save_watch_me_recording(
            target_url=url,
            user_journey_steps=steps,
            run_records=run_records,
            credentials=state.get("credentials") or {},
            sub_tasks=state.get("sub_tasks") or [],
            label=label,
            app=app,
            flow=flow,
        )
    except Exception as save_err:
        logger.warning("Failed to update Watch-me recording after replay: %s", save_err)

    rand_state = randomization.to_dict() if randomization else {}
    ledger_list = rand_state.get("ledger") or []
    if isinstance(ledger_list, dict):
        ledger_list = ledger_list.get("entries") or []
    return {
        "run_records": run_records,
        "error_log": error_log,
        "watch_me_status": "replayed",
        "randomization_state": rand_state,
        "randomization_ledger": list(ledger_list),
        "non_randomizable_endpoints": list(rand_state.get("non_randomizable") or []),
    }


async def load_saved_recording(state: AgentState) -> Dict[str, Any]:
    """Load a disk-saved Watch-me capture for analysis without re-recording.

    Chat examples:
    - ``list recordings``
    - ``analyse saved recording``
    - ``analyse saved recording opensource-demo.orangehrmlive.com``
    """
    from src.utils.recording_store import (
        format_recordings_list,
        list_recordings,
        load_watch_me_recording,
        resolve_recording_path,
    )

    logger.info("Node: load_saved_recording starting...")
    text = get_latest_human_text(state.get("messages"))
    cleaned = (text or "").strip()

    if re.search(r"\blist\s+recordings\b|\bsaved\s+recordings\b", cleaned, re.I):
        app_hint = ""
        try:
            from src.utils.app_registry import app_id_from_url

            app_hint = state.get("app") or app_id_from_url(state.get("target_url") or "")
        except Exception:
            app_hint = state.get("app") or ""
        # Optional: "list recordings for opensource-demo.orangehrmlive.com"
        m = re.search(r"\b(?:for|app|on)\s+([a-zA-Z0-9._-]+\.[a-zA-Z0-9._-]+)", cleaned)
        if m:
            app_hint = m.group(1)
        rows = list_recordings(app=app_hint)
        # Natural-language RAG suggestions when filtering by free text
        rag_note = ""
        free = re.sub(
            r"\b(list|saved|recordings?|for|app|on|the|me|please)\b",
            " ",
            cleaned,
            flags=re.I,
        )
        free = re.sub(r"\s+", " ", free).strip()
        if free and len(free) > 3:
            try:
                from src.utils.rag_store import query as rag_query

                hits = rag_query(free, app=app_hint or None, top_k=3)
                if hits:
                    sug = []
                    for hit in hits:
                        meta = hit.get("metadata") or {}
                        a = meta.get("app") or ""
                        f = meta.get("flow") or meta.get("kind") or ""
                        if a:
                            sug.append(f"`{a}/{f}`" if f else f"`{a}`")
                    if sug:
                        rag_note = "\n\n**Similar from knowledge:** " + ", ".join(sug)
            except Exception:
                pass
        return {
            "watch_me_status": "listed",
            "messages": [
                AIMessage(content=format_recordings_list(rows) + rag_note)
            ],
        }

    # Strip command words to leave host/path hint
    hint = re.sub(
        r"\b(reuse|analyse|analyze|load|use|from|saved|recording|recordings|"
        r"the|last|previous|rerun|replay)\b",
        " ",
        cleaned,
        flags=re.I,
    )
    hint = re.sub(r"\s+", " ", hint).strip()

    path = resolve_recording_path(
        hint,
        app=state.get("app") or "",
        flow=state.get("flow") or "",
    )
    if path is None:
        rows = list_recordings()
        return {
            "error_log": ["No saved Watch-me recording found."],
            "watch_me_status": "missing_recording",
            "messages": [
                AIMessage(
                    content=(
                        "No saved recording found.\n\n"
                        + format_recordings_list(rows)
                    )
                )
            ],
        }

    try:
        loaded = load_watch_me_recording(path)
    except NFESecurityError:
        raise
    except NFEError as exc:
        return {
            **node_failure_update(
                exc,
                logger=logger,
                context="load_saved_recording",
                extra={"watch_me_status": "load_failed"},
            )
        }
    except Exception as exc:
        wrapped = wrap_unexpected(
            exc,
            code=ErrorCode.RECORDING_MISSING,
            user_message=f"Could not load recording `{path}`.",
        )
        return {
            **node_failure_update(
                wrapped,
                logger=logger,
                context="load_saved_recording",
                extra={"watch_me_status": "load_failed"},
            )
        }

    runs = loaded.get("run_records") or []
    steps = loaded.get("user_journey_steps") or []
    rel = loaded.get("recording_file") or str(path)
    try:
        from pathlib import Path as _P

        root = _P(__file__).resolve().parents[2]
        p = _P(path)
        try:
            rel = str(p.relative_to(root))
        except ValueError:
            rel = str(p)
    except Exception:
        pass

    next_msg = (
        f"Loaded `{rel}` — **{len(steps)}** step(s), **{len(runs)}** run(s). "
    )
    if len(runs) >= 2:
        next_msg += "Running analysis (skipping browser record/replay)…"
        status = "ready_analyse"
    elif steps:
        next_msg += "Replaying headless for Run 2, then analysis…"
        status = "ready_replay"
    else:
        next_msg += "Recording has no steps to replay."
        status = "empty"

    return {
        **loaded,
        "error_log": [],
        "intent": "reuse_recording",
        "watch_me_status": status,
        "messages": [AIMessage(content=next_msg)],
    }


async def run_automation(state: AgentState) -> Dict[str, Any]:
    """Capture two independent executions of the planned journey.

    Run 1 records protocol traffic as-is. Before Run 2, state-mutating HTTP
    payload fields harvested from Run 1 are rewritten via route interception
    so duplicate-key errors do not pollute correlation.

    Args:
        state: State containing the target URL and planned browser steps.

    Returns:
        A partial state with run-record dictionaries, randomization ledger, and
        accumulated error strings.
    """
    import asyncio

    logger.info("Node: run_automation starting...")

    if state.get("error_log"):
        return {}

    url = state["target_url"]
    steps = state.get("user_journey_steps", [])

    recorder = PlaywrightBrowserRecorder(debug_mode=False)
    run_records = []
    error_log = list(state.get("error_log", []))

    from src.utils.model_router import allow_blocking_io
    from src.utils.data_randomization import (
        DataRandomizationMiddleware,
        build_middleware_from_run1,
    )

    randomization = None

    def _execute_run(capture_storage: bool, mw=None):
        """Execute one synchronous capture in a worker thread.

        Args:
            capture_storage: Whether to create a fresh browser context.
            mw: Optional ``DataRandomizationMiddleware`` for Run 2 payload rewrite.

        Returns:
            Recorder output containing network, timeline, cookie, and storage data.
        """
        # Playwright + optional self-heal LLM use sync I/O; allow under blockbuster.
        with allow_blocking_io():
            return recorder.execute_journey(
                url, steps, capture_storage, randomization=mw
            )

    try:
        logger.info("Executing RUN 1...")
        run1_data = await asyncio.to_thread(_execute_run, True, None)
        run_records.append({
            "run_id": 1,
            "network_requests": run1_data.get("network_requests") or [],
            "step_timeline": run1_data.get("step_timeline") or [],
            "cookies": run1_data.get("cookies") or [],
            "local_storage": run1_data.get("local_storage") or {},
            "session_storage": run1_data.get("session_storage") or {},
            "screenshot_paths": [],
        })
        if run1_data.get("error"):
            error_log.append(f"Run 1 incomplete: {run1_data['error']}")
        randomization = build_middleware_from_run1(
            run1_data.get("network_requests") or []
        )
        logger.info(
            "Run 1 payload harvest: %s transform(s)",
            len(randomization.transforms),
        )
    except NFESecurityError:
        raise
    except NFEError as e:
        from src.exceptions import log_exception

        log_exception(logger, e, context="run_automation_run1")
        error_log.append(to_error_log_entry(e))
    except Exception as e:
        wrapped = wrap_unexpected(e, user_message="Run 1 browser capture failed.")
        from src.exceptions import log_exception

        log_exception(logger, wrapped, context="run_automation_run1")
        error_log.append(to_error_log_entry(wrapped))

    if run_records and not (run_records[0].get("network_requests") is None):
        try:
            logger.info("Executing RUN 2 (with HTTP payload randomization)...")
            run2_data = await asyncio.to_thread(_execute_run, True, randomization)
            run_records.append({
                "run_id": 2,
                "network_requests": run2_data.get("network_requests") or [],
                "step_timeline": run2_data.get("step_timeline") or [],
                "cookies": run2_data.get("cookies") or [],
                "local_storage": run2_data.get("local_storage") or {},
                "session_storage": run2_data.get("session_storage") or {},
                "screenshot_paths": [],
            })
            if run2_data.get("error"):
                error_log.append(
                    to_error_log_entry(
                        NFEPipelineError(
                            str(run2_data["error"]),
                            user_message=f"Run 2 incomplete: {run2_data['error']}",
                        )
                    )
                )
            if run2_data.get("randomization_state"):
                randomization = DataRandomizationMiddleware.from_dict(
                    run2_data["randomization_state"]
                )
        except NFESecurityError:
            raise
        except NFEError as e:
            from src.exceptions import log_exception

            log_exception(logger, e, context="run_automation_run2")
            error_log.append(to_error_log_entry(e))
        except Exception as e:
            wrapped = wrap_unexpected(e, user_message="Run 2 browser capture failed.")
            from src.exceptions import log_exception

            log_exception(logger, wrapped, context="run_automation_run2")
            error_log.append(to_error_log_entry(wrapped))

    rand_state = randomization.to_dict() if randomization else {}
    ledger_list = rand_state.get("ledger") or []
    if isinstance(ledger_list, dict):
        ledger_list = ledger_list.get("entries") or []
    return {
        "run_records": run_records,
        "error_log": error_log,
        "randomization_state": rand_state,
        "randomization_ledger": list(ledger_list),
        "non_randomizable_endpoints": list(rand_state.get("non_randomizable") or []),
    }


def after_watch_me_record(
    state: AgentState,
) -> Literal["replay_recorded_journey", "__end__"]:
    """Continue to headless replay when recording produced usable steps.

    Args:
        state: State after :func:`watch_me_record`.

    Returns:
        Replay node or END when recording failed/cancelled without steps.
    """
    if state.get("watch_me_status") == "cancelled":
        return "__end__"
    if state.get("user_journey_steps"):
        return "replay_recorded_journey"
    return "__end__"


def after_load_saved_recording(
    state: AgentState,
) -> Literal["analyse_traffic", "replay_recorded_journey", "__end__"]:
    """Route a loaded recording to analysis, replay, or end.

    Args:
        state: State after :func:`load_saved_recording`.

    Returns:
        Next node or END for list/missing/empty recordings.
    """
    status = state.get("watch_me_status") or ""
    if status == "ready_analyse":
        return "analyse_traffic"
    if status == "ready_replay":
        return "replay_recorded_journey"
    return "__end__"
