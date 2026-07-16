"""Define and compile the LangGraph performance-analysis workflow."""

import logging
import re
from typing import Dict, Any, List, Literal

from langchain_core.messages import AIMessage
from langgraph.graph import StateGraph, START, END

from src.agents.state import AgentState
from src.agents.navigator_agent import NavigatorAgent
from src.agents.orchestrator_agent import OrchestratorAgent
from src.agents.parameter_agent import ParameterAgent
from src.agents.intent_router import route_user_message, get_latest_human_text
from src.agents.analysis_qa_agent import AnalysisQAAgent
from src.agents.transaction_agent import TransactionAgent
from src.tools.playwright_tool import PlaywrightBrowserRecorder
from src.agents.analyst_agent import TrafficAnalystAgent
from src.utils.validation import extract_inputs_from_message
from src.utils.formatting import format_correlation_report, build_performance_test_output
from src.utils.har_export import network_logs_to_har
from src.utils.k6_generator import generate_k6_script
from src.utils.load_test_ir import build_load_test_ir
from src.utils.artifacts import save_k6_script, save_load_test_ir
from src.utils.perf_test_classification import reconcile_analysis

from config.observability import initialize_observability

initialize_observability()

logger = logging.getLogger("AgentGraph")

try:
    from src.utils.model_router import get_model_router

    logger.info("LLM auto-routing: %s", get_model_router().routing_summary())
except Exception as _router_err:
    logger.warning("LLM router not ready: %s", _router_err)


def _has_prior_analysis(state: AgentState) -> bool:
    """Check whether state contains reusable analysis context.

    Args:
        state: Current workflow state.

    Returns:
        ``True`` when prior captures or analysis outputs are present.
    """
    return bool(
        state.get("performance_test_output")
        or state.get("dependencies")
        or state.get("correlations")
        or state.get("parameterizable_candidates")
        or state.get("transactions")
        or (state.get("target_url") and state.get("run_records"))
    )


async def route_intent(state: AgentState) -> Dict[str, Any]:
    """Classify the latest message and initialize routing state.

    Args:
        state: Current workflow state containing conversation messages.

    Returns:
        A partial state with intent, reset errors, and an optional chat reply.

    Raises:
        Exception: If intent classification fails.
    """
    logger.info("Node: route_intent starting...")
    has_prior = _has_prior_analysis(state)
    decision = await route_user_message(
        state.get("messages"),
        has_prior_analysis_context=has_prior,
    )
    updates: Dict[str, Any] = {"intent": decision.intent, "error_log": []}

    if decision.intent == "conversation":
        updates["messages"] = [
            AIMessage(content=decision.reply or "How can I help you today?")
        ]
    return updates


def after_intent_router(
    state: AgentState,
) -> Literal["respond_conversation", "answer_analysis_question", "orchestrate_journey"]:
    """Select the node that handles the classified intent.

    Args:
        state: State containing the intent set by :func:`route_intent`.

    Returns:
        The next LangGraph node name.
    """
    intent = state.get("intent", "conversation")
    if intent == "analysis_qa":
        return "answer_analysis_question"
    if intent in ("performance_analysis", "follow_up_analysis"):
        return "orchestrate_journey"
    return "respond_conversation"


async def respond_conversation(state: AgentState) -> Dict[str, Any]:
    """Terminate a conversational request after its reply is prepared.

    Args:
        state: Current workflow state; it is not modified.

    Returns:
        An empty state update.
    """
    logger.info("Node: respond_conversation (pipeline skipped).")
    return {}


async def answer_analysis_question(state: AgentState) -> Dict[str, Any]:
    """Answer a follow-up using existing analysis context.

    Args:
        state: State containing prior analysis and conversation messages.

    Returns:
        A partial state with an AI answer and any rebuilt transaction artifacts.

    Raises:
        Exception: If the analysis QA agent cannot produce an answer.
    """
    logger.info("Node: answer_analysis_question (lightweight QA only).")
    question = get_latest_human_text(state.get("messages"))
    qa = AnalysisQAAgent()

    q = (question or "").lower()
    wants_rebuild = bool(
        re.search(
            r"\b(txn|txns|transaction|transactions|k6|load\s*script|generate\s+script)\b",
            q,
        )
    )

    updates: Dict[str, Any] = {}
    answer_state = dict(state)
    if wants_rebuild and (state.get("run_records") or state.get("user_journey_steps")):
        try:
            rebuilt = await qa._rebuild_txn_and_k6(state)
            updates["transactions"] = rebuilt["transactions"]
            perf = dict(state.get("performance_test_output") or {})
            artifacts = dict(perf.get("artifacts") or {})
            artifacts["k6_script"] = rebuilt["k6_script"]
            if rebuilt.get("k6_file"):
                artifacts["k6_file"] = rebuilt["k6_file"]
            if rebuilt.get("load_test_ir"):
                artifacts["load_test_ir"] = rebuilt["load_test_ir"]
                perf["load_test_ir"] = rebuilt["load_test_ir"]
            perf["artifacts"] = artifacts
            perf["transactions"] = rebuilt["transactions"]
            updates["performance_test_output"] = perf
            answer_state.update(updates)
        except Exception as exc:
            logger.warning("Could not rebuild TXN/k6 before QA answer: %s", exc)

    answer = await qa.answer(question, answer_state)
    updates["messages"] = [AIMessage(content=answer)]
    return updates


async def orchestrate_journey(state: AgentState) -> Dict[str, Any]:
    """Extract journey inputs and decompose them into navigator tasks.

    Args:
        state: State containing the user request and optional reusable inputs.

    Returns:
        A partial state with target data and subtasks, or an error response when
        no target URL is available.

    Raises:
        Exception: If input extraction or orchestration fails.
    """
    logger.info("Node: orchestrate_journey starting...")

    allow_reuse = state.get("intent") == "follow_up_analysis"
    url, credentials, journey = await extract_inputs_from_message(
        state, allow_state_reuse=allow_reuse
    )

    if not url:
        return {
            "error_log": [
                "No target URL was provided. Please provide a valid 'target_url' or describe the target in your message."
            ],
            "messages": [
                AIMessage(
                    content=(
                        "I can run the performance analysis pipeline, but I need a **target URL** "
                        "and journey steps.\n\n"
                        "Example:\n"
                        '```json\n{\n  "target_url": "https://www.saucedemo.com/",\n'
                        '  "credentials": {"username": "standard_user", "password": "secret_sauce"},\n'
                        '  "user_journey_steps": ["Login", "Add Bolt T-Shirt to cart", "Checkout"]\n}\n```'
                    )
                )
            ],
        }

    orchestrator = OrchestratorAgent()
    sub_tasks = await orchestrator.decompose_journey(url, credentials, journey)

    return {
        "target_url": url,
        "credentials": credentials,
        "sub_tasks": sub_tasks,
        # Clear stale planned steps unless this is an explicit follow-up reuse
        "user_journey_steps": state.get("user_journey_steps") if allow_reuse else [],
        "run_records": [],
        "error_log": [],
    }


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


async def run_automation(state: AgentState) -> Dict[str, Any]:
    """Capture two independent executions of the planned journey.

    Args:
        state: State containing the target URL and planned browser steps.

    Returns:
        A partial state with run-record dictionaries and accumulated error strings.
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

    def _execute_run(capture_storage: bool):
        """Execute one synchronous capture in a worker thread.

        Args:
            capture_storage: Whether to create a fresh browser context.

        Returns:
            Recorder output containing network, timeline, cookie, and storage data.
        """
        # Playwright + optional self-heal LLM use sync I/O; allow under blockbuster.
        with allow_blocking_io():
            return recorder.execute_journey(url, steps, capture_storage)

    try:
        logger.info("Executing RUN 1...")
        run1_data = await asyncio.to_thread(_execute_run, True)
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
    except Exception as e:
        logger.error(f"Error during RUN 1: {e}")
        error_log.append(f"Run 1 failed: {str(e)}")

    if run_records and not (run_records[0].get("network_requests") is None):
        try:
            logger.info("Executing RUN 2...")
            run2_data = await asyncio.to_thread(_execute_run, True)
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
                error_log.append(f"Run 2 incomplete: {run2_data['error']}")
        except Exception as e:
            logger.error(f"Error during RUN 2: {e}")
            error_log.append(f"Run 2 failed: {str(e)}")

    return {"run_records": run_records, "error_log": error_log}


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
        except Exception as e:
            logger.warning("Extra correlation run failed: %s", e)
            error_log.append(f"Run 3 failed: {e}")
            extra_run_note = (
                f"**Extra run requested but failed:** {e}. "
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

    k6_script = generate_k6_script(
        target_url=state["target_url"],
        parameterizable_candidates=parameterizable_candidates,
        dependencies=dependencies,
        transactions=transactions,
        network_requests=run1.get("network_requests") or [],
        ir=load_test_ir,
    )

    k6_file: Dict[str, str] = {}
    try:
        k6_file = save_k6_script(k6_script, target_url=state["target_url"])
        save_load_test_ir(load_test_ir, target_url=state["target_url"])
    except Exception as art_err:
        logger.warning("Failed to write k6 artifact file: %s", art_err)

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
        include_transactions=False,
        include_k6=False,
        cookie_notes=cookie_notes,
        correlation_advice_summary=advice.summary or "",
        extra_run_note=extra_run_note,
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
        "messages": [AIMessage(content=summary_markdown)],
    }


def after_orchestrate(
    state: AgentState,
) -> Literal["plan_navigator_steps", "__end__"]:
    """Route successful orchestration to planning or terminate on errors.

    Args:
        state: State containing orchestration errors, if any.

    Returns:
        The navigator node name or LangGraph's terminal marker.
    """
    if state.get("error_log"):
        return "__end__"
    return "plan_navigator_steps"


# Node updates merge into AgentState; conditional edges choose lightweight or full flow.
workflow = StateGraph(AgentState)

workflow.add_node("route_intent", route_intent)
workflow.add_node("respond_conversation", respond_conversation)
workflow.add_node("answer_analysis_question", answer_analysis_question)
workflow.add_node("orchestrate_journey", orchestrate_journey)
workflow.add_node("plan_navigator_steps", plan_navigator_steps)
workflow.add_node("run_automation", run_automation)
workflow.add_node("analyse_traffic", analyse_traffic)

workflow.add_edge(START, "route_intent")
workflow.add_conditional_edges(
    "route_intent",
    after_intent_router,
    {
        "respond_conversation": "respond_conversation",
        "answer_analysis_question": "answer_analysis_question",
        "orchestrate_journey": "orchestrate_journey",
    },
)
workflow.add_edge("respond_conversation", END)
workflow.add_edge("answer_analysis_question", END)
workflow.add_conditional_edges(
    "orchestrate_journey",
    after_orchestrate,
    {
        "plan_navigator_steps": "plan_navigator_steps",
        "__end__": END,
    },
)
workflow.add_edge("plan_navigator_steps", "run_automation")
workflow.add_edge("run_automation", "analyse_traffic")
workflow.add_edge("analyse_traffic", END)

graph = workflow.compile()
