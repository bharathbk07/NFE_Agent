"""CLI entry that imports and runs the compiled LangGraph workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage, HumanMessage

from config.observability import initialize_observability
from config.settings import settings
from src.graph import graph

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG_MODE else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("NFE_Agent")


def _build_message_from_config(cfg: Dict[str, Any]) -> str:
    """Build a human chat message from a JSON config dict.

    Prefers an explicit ``message`` field; otherwise serializes journey fields
    so the intent router / orchestrator can extract them.
    """
    if cfg.get("message"):
        return str(cfg["message"]).strip()

    payload: Dict[str, Any] = {}
    if cfg.get("url") or cfg.get("target_url"):
        payload["target_url"] = cfg.get("target_url") or cfg.get("url")
    if cfg.get("credentials"):
        payload["credentials"] = cfg["credentials"]
    journey = cfg.get("journey") or cfg.get("user_journey_steps")
    if journey is not None:
        payload["user_journey_steps"] = journey
    if payload:
        return json.dumps(payload, indent=2)
    return "Help me analyse a web journey for performance testing."


def build_initial_state(
    message: str,
    *,
    target_url: Optional[str] = None,
    credentials: Optional[Dict[str, Any]] = None,
    user_journey_steps: Optional[Any] = None,
) -> Dict[str, Any]:
    """Build an AgentState-compatible dict for ``graph.ainvoke``."""
    state: Dict[str, Any] = {
        "messages": [HumanMessage(content=message)],
    }
    if target_url:
        state["target_url"] = target_url
    if credentials is not None:
        state["credentials"] = credentials
    if user_journey_steps is not None:
        state["user_journey_steps"] = user_journey_steps
    return state


def _final_ai_text(result: Dict[str, Any]) -> str:
    """Return the last AI message content from a graph result."""
    messages = result.get("messages") or []
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return str(msg.content or "")
        content = getattr(msg, "content", None)
        role = getattr(msg, "type", None) or getattr(msg, "role", None)
        if content and role in ("ai", "assistant"):
            return str(content)
        if isinstance(msg, dict) and msg.get("type") in ("ai", "assistant"):
            return str(msg.get("content") or "")
    return ""


async def run_graph(initial_state: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke the compiled LangGraph workflow."""
    initialize_observability()
    try:
        from src.utils.workspace import ensure_workspace

        ensure_workspace()
    except Exception as ws_err:
        logger.warning("Workspace init skipped: %s", ws_err)
    logger.info("Invoking LangGraph workflow...")
    return await graph.ainvoke(initial_state)


def main(argv: Optional[list[str]] = None) -> int:
    """Parse CLI args, run the graph, print the final reply."""
    parser = argparse.ArgumentParser(
        description="Run the NFE Agent LangGraph pipeline from the command line."
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="Path to a JSON config file, or a free-text chat message",
    )
    parser.add_argument(
        "-m",
        "--message",
        help="Chat message (overrides positional input when both are set as message)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="Optional path to write a JSON summary of the result",
    )
    args = parser.parse_args(argv)

    message: Optional[str] = args.message
    target_url: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None
    user_journey_steps: Optional[Any] = None

    if args.input and not message:
        path_or_text = args.input
        if path_or_text.endswith(".json"):
            with open(path_or_text, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            message = _build_message_from_config(cfg)
            target_url = cfg.get("target_url") or cfg.get("url")
            credentials = cfg.get("credentials")
            user_journey_steps = cfg.get("user_journey_steps") or cfg.get("journey")
        else:
            message = path_or_text

    if not message:
        parser.print_help()
        print(
            "\nExamples:\n"
            '  python -m src.main \'{"target_url":"https://example.com","user_journey_steps":["Login"]}\'\n'
            "  python -m src.main config.json -o result.json\n"
            "  python -m src.main -m 'watch me https://example.com'\n",
            file=sys.stderr,
        )
        return 2

    # If positional looks like inline JSON, prefer structured fields + message.
    if (
        not args.message
        and args.input
        and not args.input.endswith(".json")
        and args.input.strip().startswith("{")
    ):
        try:
            cfg = json.loads(args.input)
            message = _build_message_from_config(cfg)
            target_url = cfg.get("target_url") or cfg.get("url")
            credentials = cfg.get("credentials")
            user_journey_steps = cfg.get("user_journey_steps") or cfg.get("journey")
        except json.JSONDecodeError:
            pass

    initial = build_initial_state(
        message,
        target_url=target_url,
        credentials=credentials,
        user_journey_steps=user_journey_steps,
    )
    result = asyncio.run(run_graph(initial))
    reply = _final_ai_text(result)
    if reply:
        print(reply)
    else:
        logger.warning("Graph finished with no AI message.")

    if args.output:
        serializable = {
            "target_url": result.get("target_url"),
            "intent": result.get("intent"),
            "error_log": result.get("error_log") or [],
            "reply": reply,
            "k6_file": (result.get("performance_test_output") or {})
            .get("artifacts", {})
            .get("k6_file"),
            "transactions": result.get("transactions"),
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(serializable, f, indent=2, default=str)
        logger.info("Wrote summary to %s", args.output)

    return 0 if not (result.get("error_log") or []) else 1


if __name__ == "__main__":
    raise SystemExit(main())
