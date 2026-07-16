"""Normalize model messages and robustly parse JSON-shaped LLM responses."""

import json
import logging
import re
from typing import Any, List, Optional

from langchain_core.messages import BaseMessage
from langchain_core.output_parsers import JsonOutputParser
from langchain_core.exceptions import OutputParserException
from langchain_core.utils.json import parse_json_markdown

logger = logging.getLogger(__name__)


def extract_message_text(content: Any) -> str:
    """Normalize LLM message content to plain text.

    Args:
        content: String, provider block list, scalar, or ``None``.

    Returns:
        Stripped text assembled from supported text blocks.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "\n".join(part.strip() for part in parts if part and str(part).strip())
    return str(content).strip()


def parse_json_from_llm(text: str) -> Any:
    """Parse JSON while tolerating Markdown fences and surrounding prose.

    Args:
        text: Model response containing an object or array.

    Returns:
        Decoded JSON value of any JSON-compatible type.

    Raises:
        ValueError: If the response is empty.
        json.JSONDecodeError: If no valid JSON payload can be decoded.
    """
    cleaned = extract_message_text(text)
    if not cleaned:
        raise ValueError("LLM returned empty response")

    try:
        return parse_json_markdown(cleaned)
    except Exception:
        pass

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    array_match = re.search(r"(\[[\s\S]*\])", cleaned)
    if array_match:
        cleaned = array_match.group(1)

    object_match = re.search(r"(\{[\s\S]*\})", cleaned)
    if object_match and not array_match:
        cleaned = object_match.group(1)

    return json.loads(cleaned)


class RobustJsonOutputParser(JsonOutputParser):
    """LangChain JSON parser tolerant of provider message formats and fences."""

    def parse_result(self, result: List[Any], *, partial: bool = False) -> Any:
        """Parse the first generation into a JSON-compatible value.

        Args:
            result: LangChain generations or message-like values.
            partial: Compatibility flag accepted from ``JsonOutputParser``.

        Returns:
            Decoded JSON object, array, or scalar.

        Raises:
            OutputParserException: If no generation, text, or valid JSON exists.
        """
        if not result:
            raise OutputParserException("LLM returned no generations")

        generation = result[0]
        text = ""
        if isinstance(generation, BaseMessage):
            text = extract_message_text(generation.content)
        elif hasattr(generation, "text"):
            text = extract_message_text(generation.text)
        elif hasattr(generation, "message"):
            text = extract_message_text(generation.message.content)
        else:
            text = extract_message_text(generation)

        if not text:
            raise OutputParserException(
                "LLM returned empty content; cannot parse JSON",
                llm_output=text,
            )

        try:
            return parse_json_from_llm(text)
        except Exception as exc:
            raise OutputParserException(
                f"Invalid json output: {text[:200]}",
                llm_output=text,
            ) from exc


def normalize_step_list(payload: Any) -> List[dict]:
    """Normalize a raw or wrapped journey-step collection.

    Args:
        payload: List of steps or mapping containing a ``steps`` list.

    Returns:
        List containing only dictionary steps.

    Raises:
        ValueError: If the accepted list shapes are absent.
    """
    if isinstance(payload, list):
        return [step for step in payload if isinstance(step, dict)]
    if isinstance(payload, dict):
        steps = payload.get("steps")
        if isinstance(steps, list):
            return [step for step in steps if isinstance(step, dict)]
    raise ValueError("Expected a JSON array of steps or an object with a 'steps' array")


def normalize_sub_task_list(payload: Any) -> List[dict]:
    """Normalize a raw or wrapped sub-task collection.

    Args:
        payload: List of tasks or mapping containing a ``sub_tasks`` list.

    Returns:
        List containing only dictionary tasks.

    Raises:
        ValueError: If the accepted list shapes are absent.
    """
    if isinstance(payload, list):
        return [task for task in payload if isinstance(task, dict)]
    if isinstance(payload, dict):
        sub_tasks = payload.get("sub_tasks")
        if isinstance(sub_tasks, list):
            return [task for task in sub_tasks if isinstance(task, dict)]
    raise ValueError(
        "Expected a JSON array of sub-tasks or an object with a 'sub_tasks' array"
    )
