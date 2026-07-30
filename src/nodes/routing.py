"""Intent routing and lightweight chat/QA nodes."""

import logging
import re
from typing import Any, Dict, Literal

from langchain_core.messages import AIMessage

from src.agents.intent_router import get_latest_human_text, route_user_message
from src.agents.state import AgentState

logger = logging.getLogger("AgentGraph")


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
    if decision.intent == "watch_me":
        updates["recording_mode"] = "watch_me"
        updates["watch_me_status"] = "requested"
        text = get_latest_human_text(state.get("messages"))
        try:
            from src.utils.app_registry import extract_watch_me_label, resolve_app_and_flow

            label = extract_watch_me_label(text or "")
            if label:
                updates["recording_label"] = label
            url = state.get("target_url") or ""
            if not url:
                from src.agents.intent_router import URL_RE

                m = URL_RE.search(text or "")
                if m:
                    url = m.group(0)
            app, flow = resolve_app_and_flow(
                target_url=url,
                label=label or state.get("recording_label") or "",
            )
            if app:
                updates["app"] = app
            if flow:
                updates["flow"] = flow
        except Exception as exc:
            logger.debug("App/flow resolve on watch_me skipped: %s", exc)
    elif decision.intent == "reuse_recording":
        updates["recording_mode"] = "reuse"
        updates["watch_me_status"] = "requested"
    else:
        updates["recording_mode"] = None
        updates["watch_me_status"] = None

    if decision.intent == "jira_perf":
        from src.nodes.jira_story import extract_issue_key

        text = get_latest_human_text(state.get("messages"))
        key = extract_issue_key(text)
        if key:
            updates["jira_issue_key"] = key

    if decision.intent == "conversation":
        updates["messages"] = [
            AIMessage(content=decision.reply or "How can I help you today?")
        ]
    return updates


def after_intent_router(
    state: AgentState,
) -> Literal[
    "respond_conversation",
    "answer_analysis_question",
    "orchestrate_journey",
    "load_saved_recording",
    "run_jira_story",
]:
    """Select the node that handles the classified intent.

    Args:
        state: State containing the intent set by :func:`route_intent`.

    Returns:
        The next LangGraph node name.
    """
    intent = state.get("intent", "conversation")
    if intent in ("analysis_qa", "pe_assist"):
        return "answer_analysis_question"
    if intent == "reuse_recording":
        return "load_saved_recording"
    if intent == "jira_perf":
        return "run_jira_story"
    if intent in ("performance_analysis", "follow_up_analysis", "watch_me"):
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
    """Run the PE supervisor (personal assistant) for assist-path questions.

    Args:
        state: State containing prior analysis and conversation messages.

    Returns:
        A partial state with an AI answer from supervisor + specialists.
    """
    logger.info("Node: answer_analysis_question / pe_assist (PE Agent OS).")
    question = get_latest_human_text(state.get("messages"))

    # Explicit script regenerate still uses AnalysisQA rebuild helpers
    q = (question or "").lower()
    wants_rebuild = bool(
        re.search(
            r"\b("
            r"(re)?generate\s+(the\s+)?(k6|load\s*)?script|"
            r"rebuild\s+(txns?|transactions?|k6|script)|"
            r"(update|refresh)\s+(the\s+)?(k6|load\s*)?script|"
            r"regenerate\s+(txns?|transactions?)"
            r")\b",
            q,
        )
    )
    updates: Dict[str, Any] = {}
    answer_state = dict(state)
    import uuid

    thread_id = str(state.get("pe_thread_id") or "").strip() or str(uuid.uuid4())
    answer_state["pe_thread_id"] = thread_id
    updates["pe_thread_id"] = thread_id
    if wants_rebuild and (state.get("run_records") or state.get("user_journey_steps")):
        try:
            from src.agents.analysis_qa_agent import AnalysisQAAgent

            qa = AnalysisQAAgent()
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
            logger.warning("Could not rebuild TXN/k6 before PE assist: %s", exc)

    try:
        from src.agents.runtime.pe_agent import run_pe_agent

        result = await run_pe_agent(answer_state, question or "")
        answer = result.get("answer") or "I couldn’t produce an answer."
        if result.get("pending_action") is not None:
            updates["pending_action"] = result.get("pending_action")
        else:
            updates["pending_action"] = None
        if result.get("agent_authorizations") is not None:
            updates["agent_authorizations"] = result.get("agent_authorizations")
        logger.info(
            "pe_agent sources=%s tools=%s pending=%s",
            result.get("context_sources"),
            result.get("tool_calls"),
            bool(result.get("pending_action")),
        )
    except Exception as exc:
        logger.warning("PE agent failed (%s); trying tool fast-path / supervisor", exc)
        answer = None
        try:
            from src.agents.runtime.assist_fastpath import try_assist_fast_path
            from src.agents.runtime.exec_approval import (
                infer_authorizations,
                merge_authorizations,
            )

            auth = merge_authorizations(
                list(answer_state.get("agent_authorizations") or []),
                infer_authorizations(question or ""),
            )
            fast = await try_assist_fast_path(question or "", authorizations=auth)
            if fast and fast.get("answer"):
                answer = fast["answer"]
                updates["agent_authorizations"] = fast.get("agent_authorizations") or auth
                updates["pending_action"] = None
                logger.info("Recovered via assist fast-path after PE agent failure")
        except Exception as fast_exc:
            logger.warning("Assist fast-path also failed: %s", fast_exc)

        if not answer:
            try:
                from src.agents.runtime.supervisor import PESupervisor

                result = await PESupervisor().run(answer_state, question or "")
                answer = result.get("answer") or ""
            except Exception as sup_exc:
                logger.warning("PE supervisor fallback failed (%s)", sup_exc)
                answer = ""

        if not answer:
            # Never dump AnalysisQA empty-session text for PE asks
            user_msg = getattr(exc, "user_message", None)
            if isinstance(user_msg, str) and user_msg.strip():
                answer = user_msg.strip()
            else:
                answer = (
                    "LLM service not reachable (Cursor). "
                    "Check `CURSOR_API_KEY` and `CURSOR_DEFAULT_MODEL`, then try again."
                )

    updates["messages"] = [AIMessage(content=answer)]
    updates["intent"] = "pe_assist"
    return updates


# Alias used by graph / docs
run_pe_supervisor = answer_analysis_question
