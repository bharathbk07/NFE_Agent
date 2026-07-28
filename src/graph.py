"""Define and compile the LangGraph performance-analysis workflow."""

import logging

from langgraph.graph import END, START, StateGraph

from config.observability import initialize_observability
from src.agents.state import AgentState
from src.nodes import (
    after_intent_router,
    after_load_saved_recording,
    after_orchestrate,
    after_watch_me_record,
    analyse_traffic,
    answer_analysis_question,
    load_saved_recording,
    orchestrate_journey,
    plan_navigator_steps,
    replay_recorded_journey,
    respond_conversation,
    route_intent,
    run_automation,
    run_jira_story,
    watch_me_record,
)

initialize_observability()

try:
    from src.utils.workspace import ensure_workspace

    ensure_workspace()
except Exception as _ws_err:
    logging.getLogger("AgentGraph").warning("Workspace init skipped: %s", _ws_err)

logger = logging.getLogger("AgentGraph")

try:
    from src.utils.model_router import get_model_router

    logger.info("LLM auto-routing: %s", get_model_router().routing_summary())
except Exception as _router_err:
    logger.warning("LLM router not ready: %s", _router_err)


# Node updates merge into AgentState; conditional edges choose lightweight or full flow.
workflow = StateGraph(AgentState)

workflow.add_node("route_intent", route_intent)
workflow.add_node("respond_conversation", respond_conversation)
workflow.add_node("answer_analysis_question", answer_analysis_question)
workflow.add_node("run_jira_story", run_jira_story)
workflow.add_node("orchestrate_journey", orchestrate_journey)
workflow.add_node("plan_navigator_steps", plan_navigator_steps)
workflow.add_node("watch_me_record", watch_me_record)
workflow.add_node("replay_recorded_journey", replay_recorded_journey)
workflow.add_node("load_saved_recording", load_saved_recording)
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
        "load_saved_recording": "load_saved_recording",
        "run_jira_story": "run_jira_story",
    },
)
workflow.add_edge("respond_conversation", END)
workflow.add_edge("answer_analysis_question", END)
workflow.add_edge("run_jira_story", END)
workflow.add_conditional_edges(
    "orchestrate_journey",
    after_orchestrate,
    {
        "plan_navigator_steps": "plan_navigator_steps",
        "watch_me_record": "watch_me_record",
        "__end__": END,
    },
)
workflow.add_conditional_edges(
    "watch_me_record",
    after_watch_me_record,
    {
        "replay_recorded_journey": "replay_recorded_journey",
        "__end__": END,
    },
)
workflow.add_conditional_edges(
    "load_saved_recording",
    after_load_saved_recording,
    {
        "analyse_traffic": "analyse_traffic",
        "replay_recorded_journey": "replay_recorded_journey",
        "__end__": END,
    },
)
workflow.add_edge("replay_recorded_journey", "analyse_traffic")
workflow.add_edge("plan_navigator_steps", "run_automation")
workflow.add_edge("run_automation", "analyse_traffic")
workflow.add_edge("analyse_traffic", END)

graph = workflow.compile()
