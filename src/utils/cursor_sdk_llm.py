"""
LangChain chat model backed by the native Cursor SDK (no OpenAI proxy).

Uses Agent.prompt() for one-shot calls. Intended for orchestration and
navigation planning only — extraction/self_heal stay on Google Gemini.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Sequence, Type, Union

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda
from langchain_core.utils.function_calling import convert_to_json_schema
from pydantic import BaseModel, ConfigDict, Field

from src.utils.json_parsing import extract_message_text, parse_json_from_llm
from src.utils.prompt_loader import render_prompt

logger = logging.getLogger(__name__)

CursorRuntime = Literal["local", "cloud"]


def _project_root() -> str:
    """Return the repository root as an absolute path string."""
    return str(Path(__file__).resolve().parents[2])


def _coerce_messages(input_value: Any) -> List[BaseMessage]:
    """Coerce runnable input into LangChain messages.

    Args:
        input_value: Message list, prompt value, message, string, or scalar.

    Returns:
        List of LangChain messages, wrapping non-message values as human input.
    """
    if isinstance(input_value, list) and all(
        isinstance(item, BaseMessage) for item in input_value
    ):
        return list(input_value)
    if hasattr(input_value, "to_messages"):
        return list(input_value.to_messages())
    if isinstance(input_value, str):
        return [HumanMessage(content=input_value)]
    if isinstance(input_value, BaseMessage):
        return [input_value]
    return [HumanMessage(content=str(input_value))]


def _messages_to_prompt(messages: Sequence[BaseMessage]) -> str:
    """Serialize role-tagged LangChain messages for a Cursor agent.

    Args:
        messages: Ordered LangChain chat messages.

    Returns:
        Plain-text prompt with role sections separated by blank lines.
    """
    parts: List[str] = []
    for message in messages:
        text = extract_message_text(message.content)
        if not text:
            continue
        role = getattr(message, "type", "human")
        if role == "system":
            parts.append(f"System:\n{text}")
        elif role in ("human", "user"):
            parts.append(f"User:\n{text}")
        elif role in ("ai", "assistant"):
            parts.append(f"Assistant:\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts).strip()


class CursorSDKChatModel(BaseChatModel):
    """Expose one-shot native Cursor agent calls as a LangChain chat model."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model_id: str
    api_key: str
    timeout: float = 120.0
    runtime: CursorRuntime = "local"
    cloud_repo: Optional[str] = None
    workdir: str = Field(default_factory=_project_root)

    @property
    def _llm_type(self) -> str:
        """Return the LangChain model type identifier."""
        return "cursor-sdk"

    def _build_agent_options(self):
        """Build local or cloud Cursor SDK options for this model.

        Returns:
            Cursor SDK ``AgentOptions`` configured for plan-mode execution.

        Raises:
            ValueError: If cloud runtime is selected without a repository URL.
        """
        from cursor_sdk.types import AgentOptions, CloudAgentOptions, CloudRepository, LocalAgentOptions

        if self.runtime == "cloud":
            if not self.cloud_repo:
                raise ValueError(
                    "CURSOR_CLOUD_REPO is required when CURSOR_RUNTIME=cloud."
                )
            return AgentOptions(
                api_key=self.api_key,
                model=self.model_id,
                mode="plan",
                cloud=CloudAgentOptions(
                    repos=[CloudRepository(url=self.cloud_repo)],
                ),
            )

        return AgentOptions(
            api_key=self.api_key,
            model=self.model_id,
            mode="plan",
            local=LocalAgentOptions(cwd=self.workdir),
        )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Synchronously invoke a one-shot Cursor agent.

        Args:
            messages: Ordered LangChain messages to serialize.
            stop: Ignored stop sequences retained for LangChain compatibility.
            run_manager: Ignored callback manager.
            **kwargs: Ignored generation options.

        Returns:
            Single-generation ``ChatResult`` containing an ``AIMessage``.

        Raises:
            ValueError: If the serialized prompt is empty.
            RuntimeError: If the SDK call fails or returns no usable result.
        """
        del stop, run_manager, kwargs

        from cursor_sdk import Agent
        from cursor_sdk.errors import CursorAgentError

        prompt = _messages_to_prompt(messages)
        if not prompt:
            raise ValueError("Cursor SDK received an empty prompt.")

        options = self._build_agent_options()
        logger.info(
            "Cursor SDK prompt model=%s runtime=%s timeout=%ss",
            self.model_id,
            self.runtime,
            self.timeout,
        )
        try:
            result = Agent.prompt(prompt, options)
        except CursorAgentError as exc:
            raise RuntimeError(f"Cursor SDK agent error: {exc}") from exc

        if getattr(result, "status", None) == "error":
            raise RuntimeError(
                f"Cursor SDK run failed: {getattr(result, 'result', result)}"
            )

        text = (getattr(result, "result", None) or "").strip()
        if not text:
            raise RuntimeError("Cursor SDK returned an empty result.")

        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))]
        )

    def with_structured_output(
        self,
        schema: Union[Dict[str, Any], Type[BaseModel]],
        *,
        method: str = "json_schema",
        include_raw: bool = False,
        **kwargs: Any,
    ) -> Runnable[Any, Any]:
        """Wrap the model with JSON-schema output parsing.

        Args:
            schema: JSON schema mapping or Pydantic model class.
            method: Structured-output mode; ``json_schema`` and ``json_mode``
                are accepted.
            include_raw: Return raw message and parsing error alongside data.
            **kwargs: Unsupported options retained for API compatibility.

        Returns:
            Runnable producing validated Pydantic data, decoded JSON, or a
            ``raw``/``parsed``/``parsing_error`` mapping.

        Raises:
            ValueError: If options or method are unsupported.
        """
        if kwargs:
            raise ValueError(f"Unsupported kwargs for Cursor SDK structured output: {kwargs}")
        if method not in ("json_schema", "json_mode"):
            raise ValueError(
                f"Cursor SDK only supports json_schema/json_mode structured output, not {method!r}."
            )

        is_pydantic = isinstance(schema, type) and issubclass(schema, BaseModel)
        json_schema = (
            schema.model_json_schema()
            if is_pydantic
            else convert_to_json_schema(schema)
        )
        schema_hint = json.dumps(json_schema, indent=2)

        def add_schema_instruction(input_value: Any) -> List[BaseMessage]:
            """Append the schema-only response instruction to runnable input."""
            messages = _coerce_messages(input_value)
            instruction = render_prompt(
                "structured_output_json",
                json_schema=schema_hint,
            )
            if messages and messages[-1].type in ("human", "user"):
                last = messages[-1]
                merged = (
                    f"{extract_message_text(last.content)}\n\n{instruction}"
                )
                return [*messages[:-1], HumanMessage(content=merged)]
            return [*messages, HumanMessage(content=instruction)]

        def parse_structured(message: BaseMessage) -> Any:
            """Decode and optionally validate one structured model message."""
            data = parse_json_from_llm(extract_message_text(message.content))
            if is_pydantic:
                return schema.model_validate(data)
            return data

        llm_runnable: Runnable[Any, BaseMessage] = (
            RunnableLambda(add_schema_instruction) | self
        )

        if include_raw:
            def wrap_parsed(payload: Dict[str, Any]) -> Dict[str, Any]:
                """Return a structured-output payload unchanged."""
                return payload

            def invoke_with_raw(input_value: Any, config: Any = None) -> Dict[str, Any]:
                """Invoke once and preserve raw output plus any parsing error."""
                raw_message = llm_runnable.invoke(input_value, config=config)
                try:
                    parsed = parse_structured(raw_message)
                    parsing_error = None
                except Exception as exc:
                    parsed = None
                    parsing_error = exc
                return {
                    "raw": raw_message,
                    "parsed": parsed,
                    "parsing_error": parsing_error,
                }

            return RunnableLambda(invoke_with_raw)

        return llm_runnable | RunnableLambda(parse_structured)


def build_cursor_sdk_chat_model(
    *,
    model_id: str,
    api_key: str,
    timeout: float,
    runtime: Optional[str] = None,
    cloud_repo: Optional[str] = None,
    workdir: Optional[str] = None,
) -> CursorSDKChatModel:
    """Construct a configured Cursor-backed LangChain chat model.

    Args:
        model_id: Cursor model identifier.
        api_key: Cursor API key.
        timeout: Requested call timeout in seconds.
        runtime: Optional ``local`` or ``cloud`` runtime override.
        cloud_repo: Repository URL required by cloud execution.
        workdir: Local runtime working directory.

    Returns:
        Configured ``CursorSDKChatModel`` instance.

    Raises:
        ValueError: If the resolved runtime is not ``local`` or ``cloud``.
    """
    resolved_runtime: CursorRuntime = (
        (runtime or os.getenv("CURSOR_RUNTIME", "local")).strip().lower()  # type: ignore[assignment]
    )
    if resolved_runtime not in ("local", "cloud"):
        raise ValueError("CURSOR_RUNTIME must be 'local' or 'cloud'.")

    return CursorSDKChatModel(
        model_id=model_id,
        api_key=api_key,
        timeout=timeout,
        runtime=resolved_runtime,
        cloud_repo=cloud_repo or os.getenv("CURSOR_CLOUD_REPO", "").strip() or None,
        workdir=workdir or os.getenv("CURSOR_WORKDIR", "").strip() or _project_root(),
    )
