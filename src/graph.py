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

from config.observability import initialize_observability

initialize_observability()

logger = logging.getLogger("AgentGraph")


def _has_prior_analysis(state: AgentState) -> bool:
    return bool(
        state.get("performance_test_output")
        or state.get("dependencies")
        or state.get("correlations")
        or state.get("parameterizable_candidates")
        or state.get("transactions")
        or (state.get("target_url") and state.get("run_records"))
    )


async def route_intent(state: AgentState) -> Dict[str, Any]:
    """
    Gatekeeper: classify the latest user message before invoking agents.
    - conversation → chat reply
    - analysis_qa → answer from prior results (no browser)
    - performance_analysis / follow_up_analysis → full pipeline
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
    intent = state.get("intent", "conversation")
    if intent == "analysis_qa":
        return "answer_analysis_question"
    if intent in ("performance_analysis", "follow_up_analysis"):
        return "orchestrate_journey"
    return "respond_conversation"


async def respond_conversation(state: AgentState) -> Dict[str, Any]:
    """No-op terminal node for conversation; reply already added in route_intent."""
    logger.info("Node: respond_conversation (pipeline skipped).")
    return {}


async def answer_analysis_question(state: AgentState) -> Dict[str, Any]:
    """Answer follow-ups about prior analysis without re-running browser agents."""
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
    """Decomposes the user journey into sub-tasks for specialized sub-agents."""
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
    """Distributes sub-tasks to NavigatorAgent sub-agents and merges Playwright steps."""
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
    """Runs the Playwright journeys (Run 1 and Run 2) to capture clean traffic."""
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
    """Runs sub-agents to identify correlations and parameterization for performance testing."""
    logger.info("Node: analyse_traffic starting...")

    error_log = state.get("error_log", [])
    records = state.get("run_records", [])

    if len(records) < 2:
        error_summary = "\n- ".join(error_log) if error_log else "Unknown automation error."
        error_msg = f"""### ⚠️ Automation Execution Failed

Playwright was unable to complete the user journey runs successfully.

**Error Details**:
- {error_summary}

Please verify the user journey steps or selectors. If credentials are required, make sure the flow includes a login sequence.
"""
        return {"messages": [AIMessage(content=error_msg)]}

    run1 = {"network_requests": records[0]["network_requests"]}
    run2 = {"network_requests": records[1]["network_requests"]}
    user_steps = state.get("user_journey_steps", [])
    sub_tasks = state.get("sub_tasks", [])

    analyst = TrafficAnalystAgent()
    correlations, dependencies = analyst.analyze_runs(run1, run2)

    param_agent = ParameterAgent()
    parameterizable_candidates = param_agent.analyze(
        user_steps, run1["network_requests"], state.get("credentials", {})
    )

    txn_agent = TransactionAgent()
    try:
        transactions = await txn_agent.group_transactions(
            target_url=state["target_url"],
            user_steps=user_steps,
            sub_tasks=sub_tasks,
            network_requests=run1["network_requests"],
        )
    except Exception as txn_err:
        logger.warning(
            "Transaction grouping failed (%s); falling back to heuristic TXNs.",
            txn_err,
        )
        transactions = txn_agent._heuristic_group(
            run1["network_requests"], user_steps, sub_tasks
        )

    har = network_logs_to_har(run1["network_requests"])
    k6_script = generate_k6_script(
        target_url=state["target_url"],
        parameterizable_candidates=parameterizable_candidates,
        dependencies=dependencies,
        transactions=transactions,
    )

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
    )

    summary_markdown = format_correlation_report(
        user_steps=user_steps,
        run1_requests=run1["network_requests"],
        dependencies=dependencies,
        parameterizable_candidates=parameterizable_candidates,
        correlations=correlations,
        sub_tasks=sub_tasks,
        transactions=transactions,
        k6_script=k6_script,
        include_transactions=False,
        include_k6=False,
    )

    return {
        "correlations": correlations,
        "dependencies": dependencies,
        "parameterizable_candidates": parameterizable_candidates,
        "transactions": transactions,
        "performance_test_output": perf_output,
        "messages": [AIMessage(content=summary_markdown)],
    }


def after_orchestrate(
    state: AgentState,
) -> Literal["plan_navigator_steps", "__end__"]:
    if state.get("error_log"):
        return "__end__"
    return "plan_navigator_steps"


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
