import asyncio
import logging
import os
from contextlib import contextmanager
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from config.settings import settings

logger = logging.getLogger(__name__)

# Keep LLM calls from hanging forever on 503 / overloaded models.
DEFAULT_LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "45"))
DEFAULT_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))


@contextmanager
def allow_blocking_io() -> Generator[None, None, None]:
    """
    Temporarily allow sync socket I/O under LangGraph's blockbuster.

    Gemini's Python client still uses sync httpx under the hood; without this,
    langgraph dev raises BlockingError on socket.connect.
    """
    try:
        from blockbuster.blockbuster import blockbuster_skip

        token = blockbuster_skip.set(True)
        try:
            yield
        finally:
            blockbuster_skip.reset(token)
    except Exception:
        # blockbuster not installed (e.g. unit tests) — nothing to skip
        yield


def invoke_llm_sync(runnable: Any, inputs: Any, config: Optional[Dict[str, Any]] = None) -> Any:
    """Sync LLM invoke that is safe under LangGraph blockbuster."""
    with allow_blocking_io():
        if config is None:
            return runnable.invoke(inputs)
        return runnable.invoke(inputs, config=config)

class TaskType(str, Enum):
    """LLM-powered work categories in the agent pipeline."""
    ORCHESTRATION = "orchestration"
    NAVIGATION = "navigation"
    EXTRACTION = "extraction"
    SELF_HEAL = "self_heal"


REASONING_TASKS = {TaskType.ORCHESTRATION, TaskType.NAVIGATION}
FAST_TASKS = {TaskType.EXTRACTION, TaskType.SELF_HEAL}


def _model_tier(model_name: str) -> str:
    """Classify a model as reasoning-capable or fast/lightweight."""
    name = model_name.lower()
    if any(tag in name for tag in ("pro", "ultra", "thinking", "opus", "sonnet")):
        return "reasoning"
    return "fast"


def _is_retriable_llm_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "503",
        "unavailable",
        "high demand",
        "resource exhausted",
        "429",
        "timeout",
        "timed out",
        "deadline",
        "temporarily",
        "overloaded",
        "blockingerror",
        "blocking call",
    )
    return any(m in text for m in markers) or exc.__class__.__name__ == "BlockingError"


class ModelRouter:
    """
    Selects the best available LLM for each agent task, with failover.

    - One model configured  → used for every task
    - Multiple models       → preferred order per task, then failover on 503/timeout
    - Explicit LLM_TASK_ROUTING env → overrides auto-selection per task
    """

    def __init__(self):
        self._models: List[str] = settings.available_models
        self._explicit_routing: Dict[str, str] = settings.llm_task_routing
        self._cache: Dict[str, BaseChatModel] = {}

        if len(self._models) == 1:
            logger.info(f"Single model configured — using '{self._models[0]}' for all tasks.")
        else:
            logger.info(f"Multi-model mode — {len(self._models)} models available: {self._models}")

    @property
    def available_models(self) -> List[str]:
        return list(self._models)

    def candidate_models(self, task: TaskType) -> List[str]:
        """Ordered model candidates for a task (preferred first, then failover)."""
        if len(self._models) == 1:
            return list(self._models)

        task_key = task.value
        ordered: List[str] = []

        if task_key in self._explicit_routing:
            explicit = self._explicit_routing[task_key]
            if explicit in self._models:
                ordered.append(explicit)

        preferred = self.select_model(task)
        if preferred not in ordered:
            ordered.append(preferred)

        # Prefer fast/lite as failover when preferred model is overloaded.
        lite = [m for m in self._models if "lite" in m.lower()]
        flash = [m for m in self._models if "flash" in m.lower() and m not in lite]
        rest = [m for m in self._models if m not in lite and m not in flash]

        for group in (lite, flash, rest):
            for model in group:
                if model not in ordered:
                    ordered.append(model)

        return ordered

    def select_model(self, task: TaskType) -> str:
        """Pick the best primary model name for a given task type."""
        if len(self._models) == 1:
            return self._models[0]

        task_key = task.value
        if task_key in self._explicit_routing:
            explicit = self._explicit_routing[task_key]
            if explicit in self._models:
                logger.debug(f"Task '{task_key}' → explicit model '{explicit}'")
                return explicit
            logger.warning(
                f"Explicit routing for '{task_key}' references unknown model '{explicit}'. "
                "Falling back to auto-selection."
            )

        reasoning = [m for m in self._models if _model_tier(m) == "reasoning"]
        fast = [m for m in self._models if _model_tier(m) == "fast"]

        # Prefer the first configured model for reliability (usually flash-lite).
        # Explicit routing / reasoning-tier models still win when present.
        if task in REASONING_TASKS and reasoning:
            selected = reasoning[0]
        elif task in FAST_TASKS and fast:
            selected = fast[0]
        else:
            selected = self._models[0]

        logger.info(f"Task '{task_key}' → auto-selected model '{selected}'")
        return selected

    def get_llm(
        self,
        task: TaskType,
        temperature: float = 0.1,
        model_name: Optional[str] = None,
    ) -> BaseChatModel:
        """Return a cached LangChain chat model instance for the given task/model."""
        resolved = model_name or self.select_model(task)
        cache_key = f"{resolved}:{temperature}:{DEFAULT_MAX_RETRIES}:{DEFAULT_LLM_TIMEOUT_SECONDS}:nostream"

        if cache_key not in self._cache:
            self._cache[cache_key] = ChatGoogleGenerativeAI(
                model=resolved,
                google_api_key=settings.GEMINI_API_KEY,
                temperature=temperature,
                max_retries=DEFAULT_MAX_RETRIES,
                timeout=DEFAULT_LLM_TIMEOUT_SECONDS,
                # Avoid sync streaming path that triggers blockbuster on socket.connect
                disable_streaming=True,
            )

        return self._cache[cache_key]

    async def ainvoke_with_failover(
        self,
        task: TaskType,
        build_chain: Callable[[BaseChatModel], Any],
        inputs: Any,
        config: Optional[Dict[str, Any]] = None,
        temperature: float = 0.1,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        """
        Invoke a LangChain runnable built from an LLM, trying candidate models
        in order when the preferred model is overloaded / times out.

        Runs sync invoke in a worker thread with blockbuster skipped so
        Gemini's sync HTTP client does not crash langgraph dev.
        """
        timeout = timeout_seconds or DEFAULT_LLM_TIMEOUT_SECONDS
        last_error: Optional[BaseException] = None
        candidates = self.candidate_models(task)
        run_config = config or {}

        for model_name in candidates:
            llm = self.get_llm(task, temperature=temperature, model_name=model_name)
            chain = build_chain(llm)
            try:
                logger.info(f"Invoking task '{task.value}' with model '{model_name}'")

                def _sync_invoke(bound_chain=chain, bound_inputs=inputs, bound_config=run_config):
                    return invoke_llm_sync(bound_chain, bound_inputs, bound_config)

                return await asyncio.wait_for(
                    asyncio.to_thread(_sync_invoke),
                    timeout=timeout,
                )
            except Exception as exc:
                last_error = exc
                if _is_retriable_llm_error(exc) and model_name != candidates[-1]:
                    logger.warning(
                        "Model '%s' failed for task '%s' (%s). Falling over to next model.",
                        model_name,
                        task.value,
                        exc,
                    )
                    continue
                if model_name == candidates[-1]:
                    break
                logger.warning(
                    "Model '%s' failed for task '%s' (%s). Trying next candidate.",
                    model_name,
                    task.value,
                    exc,
                )

        assert last_error is not None
        raise last_error

    def routing_summary(self) -> Dict[str, str]:
        """Return task→model mapping for logging/debugging."""
        return {task.value: self.select_model(task) for task in TaskType}


_router: Optional[ModelRouter] = None


def get_model_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router
