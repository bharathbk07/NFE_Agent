"""Journey orchestration node and post-orchestrate routing."""

import logging
from typing import Any, Dict, Literal

from langchain_core.messages import AIMessage

from src.agents.orchestrator_agent import OrchestratorAgent
from src.agents.state import AgentState
from src.exceptions import (
    ErrorCode,
    NFEError,
    NFESecurityError,
    NFEValidationError,
    node_failure_update,
    wrap_unexpected,
)
from src.utils.validation import extract_inputs_from_message

logger = logging.getLogger("AgentGraph")


async def orchestrate_journey(state: AgentState) -> Dict[str, Any]:
    """Extract journey inputs and decompose them into navigator tasks.

    Args:
        state: State containing the user request and optional reusable inputs.

    Returns:
        A partial state with target data and subtasks, or an error response when
        no target URL is available.
    """
    logger.info("Node: orchestrate_journey starting...")

    is_watch_me = (
        state.get("intent") == "watch_me"
        or state.get("recording_mode") == "watch_me"
    )
    allow_reuse = state.get("intent") == "follow_up_analysis"
    try:
        url, credentials, journey = await extract_inputs_from_message(
            state, allow_state_reuse=allow_reuse
        )
    except NFESecurityError:
        raise
    except NFEError as exc:
        return node_failure_update(exc, logger=logger, context="orchestrate_journey")
    except Exception as exc:
        return node_failure_update(
            wrap_unexpected(exc),
            logger=logger,
            context="orchestrate_journey",
        )

    if not url:
        missing = NFEValidationError(
            "No target URL was provided",
            code=ErrorCode.VALIDATION,
            user_message=(
                "Watch-me needs a target URL. Include an https URL with your watch-me request."
                if is_watch_me
                else (
                    "I can run the performance analysis pipeline, but I need a **target URL** "
                    "and journey steps."
                )
            ),
        )
        if is_watch_me:
            return {
                **node_failure_update(
                    missing,
                    extra={
                        "recording_mode": "watch_me",
                        "watch_me_status": "missing_url",
                        "messages": [
                            AIMessage(
                                content=(
                                    "For **watch me** mode I need a **target URL** "
                                    "(credentials optional).\n\n"
                                    "Example:\n"
                                    "`watch me https://www.saucedemo.com/ "
                                    "username=standard_user password=secret_sauce`\n\n"
                                    "I’ll open a headed browser; click through your flow, then "
                                    "**Done recording**."
                                )
                            )
                        ],
                    },
                ),
            }
        return {
            **node_failure_update(
                missing,
                extra={
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
                },
            ),
        }

    if is_watch_me:
        # Skip LLM journey decomposition — the user will click in a headed browser.
        sub_tasks = [{
            "name": "watch_me_flow",
            "description": "User-driven interactive recording (Watch-me)",
            "focus": "general",
        }]
        cred_hint = ""
        if credentials:
            keys = ", ".join(sorted(credentials.keys()))
            cred_hint = f"\n\nCredentials on hand ({keys}) — use them in the browser if the site asks."
        return {
            "target_url": url,
            "credentials": credentials,
            "sub_tasks": sub_tasks,
            "user_journey_steps": [],
            "run_records": [],
            "error_log": [],
            "recording_mode": "watch_me",
            "watch_me_status": "ready",
            "messages": [
                AIMessage(
                    content=(
                        f"Opening a **headed** browser at `{url}`. "
                        "Click through your journey, then press **Done recording** "
                        f"in the page overlay.{cred_hint}"
                    )
                )
            ],
        }

    orchestrator = OrchestratorAgent()
    try:
        sub_tasks = await orchestrator.decompose_journey(url, credentials, journey)
    except NFESecurityError:
        raise
    except NFEError as exc:
        return node_failure_update(exc, logger=logger, context="orchestrate_journey")
    except Exception as exc:
        return node_failure_update(
            wrap_unexpected(exc, user_message="Journey orchestration failed."),
            logger=logger,
            context="orchestrate_journey",
        )

    return {
        "target_url": url,
        "credentials": credentials,
        "sub_tasks": sub_tasks,
        # Clear stale planned steps unless this is an explicit follow-up reuse
        "user_journey_steps": state.get("user_journey_steps") if allow_reuse else [],
        "run_records": [],
        "error_log": [],
    }


def after_orchestrate(
    state: AgentState,
) -> Literal["plan_navigator_steps", "watch_me_record", "__end__"]:
    """Route successful orchestration to planning, Watch-me, or terminate on errors.

    Args:
        state: State containing orchestration errors, if any.

    Returns:
        The next node name or LangGraph's terminal marker.
    """
    if state.get("error_log"):
        return "__end__"
    if (
        state.get("intent") == "watch_me"
        or state.get("recording_mode") == "watch_me"
    ):
        return "watch_me_record"
    return "plan_navigator_steps"
