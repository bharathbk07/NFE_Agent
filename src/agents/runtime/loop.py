"""Bounded tool/LLM loop for PE specialists."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.utils.model_router import TaskType, get_model_router

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3


async def run_tool_loop(
    *,
    system: str,
    user: str,
    tools: Sequence[Any],
    max_rounds: int = MAX_TOOL_ROUNDS,
) -> Dict[str, Any]:
    """Run a bounded bind_tools loop and return the final text + tool names.

    Args:
        system: System prompt.
        user: User / goal prompt.
        tools: LangChain tools (may be empty).
        max_rounds: Max tool-calling rounds.

    Returns:
        ``{text, tool_calls}`` where tool_calls is a list of tool names used.
    """
    router = get_model_router()
    tool_names_used: List[str] = []
    messages: List[Any] = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]

    if not tools:
        text = await router.ainvoke_with_failover(
            TaskType.ASSIST,
            lambda model: model,
            messages,
        )
        return {"text": _content_to_str(text), "tool_calls": []}

    tool_map = {getattr(t, "name", ""): t for t in tools if getattr(t, "name", "")}

    for _round in range(max(1, max_rounds)):
        ai = await router.ainvoke_with_failover(
            TaskType.ASSIST,
            lambda model: model.bind_tools(list(tools)),
            messages,
        )
        if not isinstance(ai, AIMessage):
            # Some adapters return plain strings
            return {"text": _content_to_str(ai), "tool_calls": tool_names_used}

        messages.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            return {"text": _content_to_str(ai), "tool_calls": tool_names_used}

        for call in calls:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            call_id = (
                call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
            ) or name
            tool = tool_map.get(name or "")
            tool_names_used.append(str(name or ""))
            if tool is None:
                result = json.dumps({"error": f"unknown tool: {name}"})
            else:
                try:
                    raw = await tool.ainvoke(args or {})
                    result = raw if isinstance(raw, str) else json.dumps(raw, default=str)
                except Exception as exc:
                    logger.warning("Tool %s failed: %s", name, exc)
                    result = json.dumps({"error": str(exc)})
            messages.append(
                ToolMessage(content=str(result)[:8000], tool_call_id=str(call_id))
            )

    # Final pass without tools to force an answer
    final = await router.ainvoke_with_failover(
        TaskType.ASSIST,
        lambda model: model,
        messages
        + [
            HumanMessage(
                content="Respond now with the final answer in markdown. Do not call tools."
            )
        ],
    )
    return {"text": _content_to_str(final), "tool_calls": tool_names_used}


def _content_to_str(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(msg)
