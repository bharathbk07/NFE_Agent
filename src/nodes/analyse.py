"""Traffic analysis and k6 artifact generation node."""

import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage

from src.agents.analyst_agent import TrafficAnalystAgent
from src.agents.parameter_agent import ParameterAgent
from src.agents.state import AgentState
from src.agents.transaction_agent import TransactionAgent
from src.exceptions import (
    ErrorCode,
    log_exception,
    to_error_log_entry,
    wrap_unexpected,
)
from src.tools.playwright_tool import PlaywrightBrowserRecorder
from src.utils.artifacts import save_k6_script, save_load_test_ir
from src.utils.formatting import build_performance_test_output, format_correlation_report
from src.utils.har_export import network_logs_to_har
from src.utils.k6_generator import generate_k6_script
from src.utils.load_test_ir import build_load_test_ir
from src.utils.perf_test_classification import reconcile_analysis

logger = logging.getLogger("AgentGraph")


async def analyse_traffic(state: AgentState) -> Dict[str, Any]:
    """Analyze captures and generate performance-test artifacts.

    Args:
        state: State containing at least two browser run records.

    Returns:
        A partial state with correlations, dependencies, parameters, transactions,
        artifacts, and a summary message. Returns only an error message when fewer
        than two captures are available.

    Raises:
        Exception: If core traffic analysis or artifact generation fails.
    """
    import asyncio

    logger.info("Node: analyse_traffic starting...")

    error_log = list(state.get("error_log", []))
    records = list(state.get("run_records", []) or [])

    if len(records) < 2:
        error_summary = "\n- ".join(error_log) if error_log else "Unknown automation error."
        error_msg = f"""### ⚠️ Automation Execution Failed

Playwright was unable to complete the user journey runs successfully.

**Error Details**:
- {error_summary}

Please verify the user journey steps or selectors. If credentials are required, make sure the flow includes a login sequence.
"""
        return {"messages": [AIMessage(content=error_msg)]}

    user_steps = state.get("user_journey_steps", [])
    sub_tasks = state.get("sub_tasks", [])
    credentials = state.get("credentials", {}) or {}

    from src.agents.correlation_classifier_agent import (
        CorrelationClassifierAgent,
        apply_correlation_advice,
    )
    from src.utils.model_router import allow_blocking_io

    def _analyze_pair(run_a: Dict[str, Any], run_b: Dict[str, Any]):
        """Analyze and reconcile a pair of capture records.

        Args:
            run_a: First capture record.
            run_b: Independent comparison capture record.

        Returns:
            A tuple of parameter candidates, correlations, and dependencies.
        """
        analyst = TrafficAnalystAgent()
        corrs, deps = analyst.analyze_runs(
            {"network_requests": run_a.get("network_requests") or []},
            {"network_requests": run_b.get("network_requests") or []},
        )
        param_agent = ParameterAgent()
        params = param_agent.analyze(
            user_steps, run_a.get("network_requests") or [], credentials
        )
        params, corrs, deps = reconcile_analysis(
            user_steps=user_steps,
            parameterizable_candidates=params,
            correlations=corrs,
            dependencies=deps,
            run1_requests=run_a.get("network_requests") or [],
            run2_requests=run_b.get("network_requests") or [],
            credentials=credentials,
        )
        return params, corrs, deps

    run1 = records[0]
    run2 = records[1]
    parameterizable_candidates, correlations, dependencies = _analyze_pair(run1, run2)

    # Protocol value-mapping ledger: deliberate test-data randomization must not
    # be treated as server-generated correlation tokens (CSRF / session / IDs).
    from src.utils.data_randomization import (
        apply_randomization_to_ir,
        build_middleware_from_run1,
        filter_correlations_against_ledger,
        filter_dependencies_against_ledger,
    )

    randomization_state = state.get("randomization_state") or {}
    randomization_ledger = list(state.get("randomization_ledger") or [])
    non_randomizable_endpoints = list(state.get("non_randomizable_endpoints") or [])
    if randomization_state.get("ledger") and not randomization_ledger:
        raw_ledger = randomization_state["ledger"]
        if isinstance(raw_ledger, list):
            randomization_ledger = list(raw_ledger)
        elif isinstance(raw_ledger, dict):
            randomization_ledger = list(raw_ledger.get("entries") or [])
    if randomization_state.get("non_randomizable") and not non_randomizable_endpoints:
        non_randomizable_endpoints = list(randomization_state.get("non_randomizable") or [])
    if not randomization_ledger and run1.get("network_requests"):
        # Reuse / navigator paths that skipped live Run-2 mutation still need a
        # harvest so IR can flag vars + non-randomizable routes.
        _mw = build_middleware_from_run1(run1.get("network_requests") or [])
        randomization_state = _mw.to_dict()
        randomization_ledger = _mw.ledger_entries()
        non_randomizable_endpoints = _mw.non_randomizable_routes()

    correlations = filter_correlations_against_ledger(
        correlations, randomization_ledger
    )
    dependencies = filter_dependencies_against_ledger(
        dependencies, randomization_ledger
    )

    classifier = CorrelationClassifierAgent()
    advice = await classifier.classify(
        target_url=state["target_url"],
        user_steps=user_steps,
        credentials=credentials,
        run1=run1,
        run2=run2,
        parameterizable_candidates=parameterizable_candidates,
        correlations=correlations,
        dependencies=dependencies,
        sub_tasks=sub_tasks,
    )
    parameterizable_candidates, correlations, dependencies = apply_correlation_advice(
        advice=advice,
        user_steps=user_steps,
        parameterizable_candidates=parameterizable_candidates,
        correlations=correlations,
        dependencies=dependencies,
    )
    correlations = filter_correlations_against_ledger(correlations, randomization_ledger)
    dependencies = filter_dependencies_against_ledger(dependencies, randomization_ledger)

    extra_run_note = ""
    if advice.needs_extra_run and len(records) < 3:
        reason = advice.extra_run_reason or "LLM requested another capture to confirm correlations"
        logger.info("Executing RUN 3 (extra correlation probe): %s", reason)
        extra_run_note = f"**Extra run performed:** {reason}"
        try:
            recorder = PlaywrightBrowserRecorder(debug_mode=False)
            url = state["target_url"]
            steps = user_steps

            def _execute_run():
                """Execute the optional third capture with blocking I/O allowed.

                Returns:
                    Recorder output containing network, timeline, cookie, and
                    storage data.
                """
                with allow_blocking_io():
                    return recorder.execute_journey(url, steps, True)

            run3_data = await asyncio.to_thread(_execute_run)
            records.append(
                {
                    "run_id": 3,
                    "network_requests": run3_data.get("network_requests") or [],
                    "step_timeline": run3_data.get("step_timeline") or [],
                    "cookies": run3_data.get("cookies") or [],
                    "local_storage": run3_data.get("local_storage") or {},
                    "session_storage": run3_data.get("session_storage") or {},
                    "screenshot_paths": [],
                }
            )
            if run3_data.get("error"):
                error_log.append(f"Run 3 incomplete: {run3_data['error']}")

            # Re-diff Run 1 vs Run 3 (fresh independent session) then re-classify once
            run3 = records[-1]
            parameterizable_candidates, correlations, dependencies = _analyze_pair(
                run1, run3
            )
            advice = await classifier.classify(
                target_url=state["target_url"],
                user_steps=user_steps,
                credentials=credentials,
                run1=run1,
                run2=run3,
                parameterizable_candidates=parameterizable_candidates,
                correlations=correlations,
                dependencies=dependencies,
                sub_tasks=sub_tasks,
            )
            # Force no further runs
            advice.needs_extra_run = False
            parameterizable_candidates, correlations, dependencies = (
                apply_correlation_advice(
                    advice=advice,
                    user_steps=user_steps,
                    parameterizable_candidates=parameterizable_candidates,
                    correlations=correlations,
                    dependencies=dependencies,
                )
            )
            correlations = filter_correlations_against_ledger(
                correlations, randomization_ledger
            )
            dependencies = filter_dependencies_against_ledger(
                dependencies, randomization_ledger
            )
        except Exception as e:
            wrapped = wrap_unexpected(
                e,
                code=ErrorCode.PIPELINE,
                user_message="Extra correlation run failed.",
            )
            log_exception(logger, wrapped, context="analyse_extra_run")
            error_log.append(to_error_log_entry(wrapped))
            extra_run_note = (
                f"**Extra run requested but failed:** {to_error_log_entry(wrapped)}. "
                "Cookie / correlation notes below still apply."
            )
    elif advice.needs_extra_run:
        extra_run_note = (
            f"**Extra run suggested:** {advice.extra_run_reason or 're-run to confirm'} "
            "(already have 3 captures — using existing evidence)."
        )

    cookie_notes = [
        n.model_dump() if hasattr(n, "model_dump") else n
        for n in (advice.cookie_notes or [])
    ]

    txn_agent = TransactionAgent()
    try:
        transactions = await txn_agent.group_transactions(
            target_url=state["target_url"],
            user_steps=user_steps,
            sub_tasks=sub_tasks,
            network_requests=run1.get("network_requests") or [],
        )
    except Exception as txn_err:
        logger.warning(
            "Transaction grouping failed (%s); falling back to heuristic TXNs.",
            txn_err,
        )
        transactions = txn_agent._heuristic_group(
            run1.get("network_requests") or [], user_steps, sub_tasks
        )

    har = network_logs_to_har(run1.get("network_requests") or [])
    load_test_ir = build_load_test_ir(
        target_url=state["target_url"],
        parameterizable_candidates=parameterizable_candidates,
        dependencies=dependencies,
        transactions=transactions,
        network_requests=run1.get("network_requests") or [],
    )
    # Attach cookie advice into IR for emitters / QA
    load_test_ir["cookie_notes"] = cookie_notes
    load_test_ir["correlation_advice_summary"] = advice.summary
    # Flag non-randomizable HTTP nodes + mark randomized vars for the k6 compiler
    load_test_ir = apply_randomization_to_ir(
        load_test_ir,
        ledger=randomization_ledger,
        non_randomizable=non_randomizable_endpoints,
    )

    k6_script = generate_k6_script(
        target_url=state["target_url"],
        parameterizable_candidates=parameterizable_candidates,
        dependencies=dependencies,
        transactions=transactions,
        network_requests=run1.get("network_requests") or [],
        ir=load_test_ir,
    )

    k6_file: Dict[str, str] = {}
    smoke_result: Dict[str, Any] = {}
    heal_notes: List[str] = []
    names = None
    try:
        from src.utils.k6_mcp import run_k6_smoke_preferred
        from src.utils.k6_healer import heal_load_test_ir, format_smoke_section

        try:
            from src.utils.artifacts import stable_artifact_names

            names = stable_artifact_names(state["target_url"])
        except Exception:
            names = None

        k6_file = save_k6_script(
            k6_script,
            target_url=state["target_url"],
            filename=(names or {}).get("script"),
        )
        save_load_test_ir(
            load_test_ir,
            target_url=state["target_url"],
            filename=(names or {}).get("ir"),
        )

        smoke_result = await run_k6_smoke_preferred(k6_file.get("path") or "")
        max_heals = 2
        attempt = 0
        while (
            not smoke_result.get("ok")
            and not smoke_result.get("skipped")
            and attempt < max_heals
        ):
            attempt += 1
            load_test_ir, notes = heal_load_test_ir(
                load_test_ir, smoke_result, attempt=attempt
            )
            heal_notes.extend(notes)
            k6_script = generate_k6_script(
                target_url=state["target_url"],
                parameterizable_candidates=parameterizable_candidates,
                dependencies=dependencies,
                transactions=transactions,
                network_requests=run1.get("network_requests") or [],
                ir=load_test_ir,
            )
            # Overwrite the same script/IR for this flow (no extra artifacts).
            k6_file = save_k6_script(
                k6_script,
                target_url=state["target_url"],
                filename=k6_file.get("filename") or (names or {}).get("script"),
            )
            save_load_test_ir(
                load_test_ir,
                target_url=state["target_url"],
                filename=(names or {}).get("ir"),
            )
            smoke_result = await run_k6_smoke_preferred(k6_file.get("path") or "")
            if smoke_result.get("ok"):
                heal_notes.append(f"Smoke passed after heal attempt {attempt}.")
                break
    except Exception as art_err:
        logger.warning("Failed to write/validate k6 artifact: %s", art_err)
        if not k6_file:
            try:
                k6_file = save_k6_script(k6_script, target_url=state["target_url"])
                save_load_test_ir(load_test_ir, target_url=state["target_url"])
            except Exception:
                pass

    perf_output = build_performance_test_output(
        target_url=state["target_url"],
        user_steps=user_steps,
        sub_tasks=sub_tasks,
        correlations=correlations,
        dependencies=dependencies,
        parameterizable_candidates=parameterizable_candidates,
        transactions=transactions,
        har=har,
        k6_script=k6_script,
        load_test_ir=load_test_ir,
        k6_file=k6_file,
    )
    perf_output["cookie_correlation_notes"] = cookie_notes
    if advice.summary:
        perf_output["correlation_advice_summary"] = advice.summary
    if smoke_result:
        perf_output["k6_smoke"] = {
            "ok": smoke_result.get("ok"),
            "skipped": smoke_result.get("skipped"),
            "summary": smoke_result.get("summary"),
            "heal_notes": heal_notes,
            "html_report": smoke_result.get("html_report") or "",
            "summary_json": smoke_result.get("summary_json") or "",
            "exit_code": smoke_result.get("exit_code"),
            "failed_checks": list(smoke_result.get("failed_checks") or []),
            "failed_urls": list(smoke_result.get("failed_urls") or []),
            "status_counts": dict(smoke_result.get("status_counts") or {}),
        }

    # Resolve IR path for Confluence attachments
    ir_path = ""
    try:
        from src.utils.artifacts import artifacts_dir, stable_artifact_names

        ir_name = (names or stable_artifact_names(state.get("target_url") or "")).get(
            "ir"
        )
        if ir_name:
            candidate = artifacts_dir() / ir_name
            if candidate.is_file():
                ir_path = str(candidate)
    except Exception:
        ir_path = ""

    confluence_info: Dict[str, Any] = {"published": False}
    if not state.get("skip_confluence_publish"):
        try:
            from src.integrations.confluence import try_publish_run_results

            confluence_info = try_publish_run_results(
                {
                    "target_url": state.get("target_url") or "",
                    "recording_file": state.get("recording_file") or "",
                    "jira_issue_key": state.get("jira_issue_key") or "",
                    "k6_path": (k6_file or {}).get("path") or "",
                    "ir_path": ir_path,
                    "html_report": (smoke_result or {}).get("html_report") or "",
                    "summary_json": (smoke_result or {}).get("summary_json") or "",
                    "smoke_result": smoke_result,
                    "smoke_ok": (smoke_result or {}).get("ok"),
                    "smoke_summary": (smoke_result or {}).get("summary") or "",
                    "heal_notes": heal_notes,
                    "transactions": transactions,
                    "failed_checks": list((smoke_result or {}).get("failed_checks") or []),
                    "failed_urls": list((smoke_result or {}).get("failed_urls") or []),
                    "status_counts": dict(
                        (smoke_result or {}).get("status_counts") or {}
                    ),
                    "exit_code": (smoke_result or {}).get("exit_code"),
                }
            )
        except Exception as conf_err:
            logger.warning("Confluence publish skipped: %s", conf_err)
            confluence_info = {
                "published": False,
                "skipped_reason": str(conf_err),
            }
    perf_output["confluence"] = confluence_info

    summary_markdown = format_correlation_report(
        user_steps=user_steps,
        run1_requests=run1.get("network_requests") or [],
        dependencies=dependencies,
        parameterizable_candidates=parameterizable_candidates,
        correlations=correlations,
        sub_tasks=sub_tasks,
        transactions=transactions,
        k6_script=k6_script,
        k6_file=k6_file,
        include_transactions=True,
        include_k6=True,
        cookie_notes=cookie_notes,
        correlation_advice_summary=advice.summary or "",
        extra_run_note="",  # never expose LLM/process internals in chat
        smoke_result=smoke_result,
        heal_notes=heal_notes,
        brief=True,
    )
    if confluence_info.get("published") and confluence_info.get("run_url"):
        summary_markdown = (
            f"{summary_markdown}\n\n"
            f"**Confluence:** [{confluence_info.get('run_title') or 'Run report'}]"
            f"({confluence_info['run_url']})"
        )
    elif confluence_info.get("skipped_reason"):
        logger.info(
            "Confluence not published: %s", confluence_info.get("skipped_reason")
        )

    return {
        "run_records": records,
        "correlations": correlations,
        "dependencies": dependencies,
        "parameterizable_candidates": parameterizable_candidates,
        "transactions": transactions,
        "performance_test_output": perf_output,
        "correlation_advice": advice.model_dump(),
        "cookie_correlation_notes": cookie_notes,
        "error_log": error_log,
        "randomization_state": randomization_state,
        "randomization_ledger": randomization_ledger,
        "non_randomizable_endpoints": non_randomizable_endpoints,
        "messages": [AIMessage(content=summary_markdown)],
    }
