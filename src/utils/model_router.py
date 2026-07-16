"""Route LLM tasks across configured providers with cached clients and failover."""

import asyncio
import logging
import os
from contextlib import contextmanager
from enum import Enum
from typing import Any, Callable, Dict, Generator, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel

from config.settings import settings
from src.utils.llm_registry import (
    DEFAULT_LLM_TIMEOUT_SECONDS,
    ModelSpec,
    build_chat_model,
    capability_score,
    is_fast_model,
    is_reasoning_model,
    parse_model_list,
    timeout_for_task_name,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))
_TIMEOUT_SLACK_SECONDS = 15.0


@contextmanager
def allow_blocking_io() -> Generator[None, None, None]:
    """Temporarily permit synchronous socket I/O under LangGraph blockbuster.

    Yields:
        No value; restores the previous blockbuster context on exit.
    """
    try:
        from blockbuster.blockbuster import blockbuster_skip

        token = blockbuster_skip.set(True)
        try:
            yield
        finally:
            blockbuster_skip.reset(token)
    except Exception:
        yield


def invoke_llm_sync(runnable: Any, inputs: Any, config: Optional[Dict[str, Any]] = None) -> Any:
    """Invoke a synchronous runnable while allowing its blocking I/O.

    Args:
        runnable: LangChain-compatible object exposing ``invoke``.
        inputs: Input accepted by the runnable.
        config: Optional LangChain invocation configuration.

    Returns:
        Arbitrary runnable result.
    """
    with allow_blocking_io():
        if config is None:
            return runnable.invoke(inputs)
        return runnable.invoke(inputs, config=config)


class TaskType(str, Enum):
    """LLM-powered work categories used for model eligibility and ranking."""
    ORCHESTRATION = "orchestration"
    NAVIGATION = "navigation"
    EXTRACTION = "extraction"
    SELF_HEAL = "self_heal"


REASONING_TASKS = {TaskType.ORCHESTRATION, TaskType.NAVIGATION}
FAST_TASKS = {TaskType.EXTRACTION, TaskType.SELF_HEAL}
# Cursor SDK is wired for planning tasks only — never extraction/self_heal.
CURSOR_ALLOWED_TASKS = REASONING_TASKS


def _is_retriable_llm_error(exc: BaseException) -> bool:
    """Classify transient provider failures as eligible for model failover.

    Args:
        exc: Exception raised during model setup or invocation.

    Returns:
        ``True`` for overload, timeout, gateway, network, or blocking failures.
    """
    name = exc.__class__.__name__
    if name in (
        "ServerError",
        "APIError",
        "DeadlineExceeded",
        "ServiceUnavailable",
        "ResourceExhausted",
        "TooManyRequests",
        "APITimeoutError",
        "RateLimitError",
        "CursorAgentError",
        "NetworkError",
    ):
        return True

    text = str(exc).lower()
    markers = (
        "503",
        "504",
        "502",
        "500",
        "unavailable",
        "high demand",
        "resource exhausted",
        "429",
        "timeout",
        "timed out",
        "deadline",
        "deadline_exceeded",
        "temporarily",
        "overloaded",
        "blockingerror",
        "blocking call",
        "connection refused",
        "connect error",
        "connection error",
        "cursor sdk",
        "rate limit",
        "cursoragenterror",
    )
    return any(m in text for m in markers) or name == "BlockingError"


def _resolve_routing_ref(raw: str, specs: List[ModelSpec]) -> Optional[ModelSpec]:
    """Resolve an explicit routing reference against configured models.

    Args:
        raw: Prefixed or bare configured model reference.
        specs: Eligible model specifications.

    Returns:
        Matching specification, or ``None`` when unresolved.
    """
    if not raw:
        return None
    try:
        parsed = ModelSpec.parse(raw)
        for spec in specs:
            if spec.ref == parsed.ref:
                return spec
            if spec.model_id == parsed.model_id and spec.provider == parsed.provider:
                return spec
    except ValueError:
        pass
    for spec in specs:
        if spec.model_id == raw or spec.ref == raw:
            return spec
    return None


def _specs_for_task(specs: List[ModelSpec], task: TaskType) -> List[ModelSpec]:
    """Filter models according to provider restrictions for a task.

    Args:
        specs: Configured viable model specifications.
        task: Pipeline task category.

    Returns:
        Eligible specifications, excluding Cursor from fast extraction tasks.
    """
    if task in CURSOR_ALLOWED_TASKS:
        return list(specs)
    return [s for s in specs if s.provider != "cursor"]


class ModelRouter:
    """Select, cache, and fail over Google and Cursor chat models by task."""

    def __init__(self):
        """Load configured models, routing overrides, and an empty client cache.

        Raises:
            RuntimeError: If no viable models are configured.
        """
        self._specs: List[ModelSpec] = parse_model_list()
        self._explicit_routing: Dict[str, str] = settings.llm_task_routing
        self._cache: Dict[str, BaseChatModel] = {}
        self._spec_by_ref: Dict[str, ModelSpec] = {s.ref: s for s in self._specs}

        if not self._specs:
            raise RuntimeError(
                "No LLM models configured. Set LLM_MODELS or GEMINI_MODEL in .env"
            )

        if len(self._specs) == 1:
            logger.info("Single model configured — using '%s' for all tasks.", self._specs[0].ref)
        else:
            logger.info(
                "Multi-model mode — %s models: %s",
                len(self._specs),
                [s.ref for s in self._specs],
            )
        logger.info("Auto routing: %s", self.routing_summary())

    @property
    def available_models(self) -> List[str]:
        """Return configured normalized model references in input order."""
        return [s.ref for s in self._specs]

    def _spec_for_ref(self, ref: str) -> ModelSpec:
        """Resolve a model reference from this router's configured set.

        Args:
            ref: Bare or provider-prefixed model reference.

        Returns:
            Configured model specification.

        Raises:
            KeyError: If the model is not configured.
            ValueError: If the reference itself is invalid.
        """
        if ref in self._spec_by_ref:
            return self._spec_by_ref[ref]
        parsed = ModelSpec.parse(ref)
        if parsed.ref in self._spec_by_ref:
            return self._spec_by_ref[parsed.ref]
        raise KeyError(f"Model '{ref}' is not in LLM_MODELS")

    def candidate_models(self, task: TaskType) -> List[str]:
        """Order eligible models for primary selection and failover.

        Args:
            task: Pipeline task category.

        Returns:
            Normalized model references, preferred model first.

        Raises:
            RuntimeError: If no provider is eligible for the task.
        """
        eligible = _specs_for_task(self._specs, task)
        if not eligible:
            raise RuntimeError(
                f"No eligible models for task '{task.value}'. "
                "Add a google: model to LLM_MODELS for extraction/self_heal."
            )
        if len(eligible) == 1:
            return [eligible[0].ref]

        task_key = task.value
        ordered: List[ModelSpec] = []

        if task_key in self._explicit_routing:
            explicit = _resolve_routing_ref(self._explicit_routing[task_key], eligible)
            if explicit:
                ordered.append(explicit)
            else:
                logger.warning(
                    "LLM_TASK_ROUTING[%s]=%r not found in LLM_MODELS — ignoring.",
                    task_key,
                    self._explicit_routing[task_key],
                )

        primary = self._spec_for_ref(self.select_model(task))
        if primary not in ordered:
            ordered.append(primary)

        if task in REASONING_TASKS:
            rest = sorted(
                [s for s in eligible if s not in ordered],
                key=capability_score,
                reverse=True,
            )
        else:
            rest = sorted(
                [s for s in eligible if s not in ordered],
                key=capability_score,
            )

        for spec in rest:
            if spec not in ordered:
                ordered.append(spec)

        return [s.ref for s in ordered]

    def select_model(self, task: TaskType) -> str:
        """Pick the primary model reference for a task.

        Args:
            task: Pipeline task category.

        Returns:
            Explicitly routed model or deterministic capability-ranked choice.

        Raises:
            RuntimeError: If no provider is eligible for the task.
        """
        eligible = _specs_for_task(self._specs, task)
        if not eligible:
            raise RuntimeError(
                f"No eligible models for task '{task.value}'. "
                "Add a google: model to LLM_MODELS for extraction/self_heal."
            )
        if len(eligible) == 1:
            return eligible[0].ref

        task_key = task.value
        if task_key in self._explicit_routing:
            explicit = _resolve_routing_ref(self._explicit_routing[task_key], eligible)
            if explicit:
                logger.debug("Task '%s' → explicit model '%s'", task_key, explicit.ref)
                return explicit.ref

        reasoning = [s for s in eligible if is_reasoning_model(s)]
        fast = [s for s in eligible if is_fast_model(s)]

        if task in REASONING_TASKS:
            pool = reasoning if reasoning else eligible
            selected = max(pool, key=capability_score)
        elif task in FAST_TASKS:
            pool = fast if fast else eligible
            selected = min(pool, key=capability_score)
        else:
            selected = eligible[0]

        logger.info(
            "Task '%s' → auto-selected '%s' (score=%s)",
            task_key,
            selected.ref,
            capability_score(selected),
        )
        return selected.ref

    def get_llm(
        self,
        task: TaskType,
        temperature: float = 0.1,
        model_name: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> BaseChatModel:
        """Return a cached LangChain model for a task and configuration.

        Args:
            task: Pipeline task category.
            temperature: Sampling temperature included in the cache key.
            model_name: Optional configured model override.
            timeout_seconds: Optional request timeout.

        Returns:
            Provider-specific cached ``BaseChatModel``.
        """
        ref = model_name or self.select_model(task)
        spec = self._spec_for_ref(ref)
        call_timeout = timeout_seconds or timeout_for_task_name(task.value)
        cache_key = (
            f"{spec.ref}:{temperature}:{DEFAULT_MAX_RETRIES}:"
            f"{call_timeout}:nostream"
        )

        if cache_key not in self._cache:
            self._cache[cache_key] = build_chat_model(
                spec, temperature=temperature, timeout=call_timeout
            )
            logger.debug("Created LLM client for %s (timeout=%ss)", spec.ref, call_timeout)

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
        """Invoke asynchronously with ordered cross-model failover.

        Args:
            task: Pipeline task category.
            build_chain: Callback that binds a model into a runnable.
            inputs: Arbitrary runnable input.
            config: Optional LangChain run configuration.
            temperature: Sampling temperature.
            timeout_seconds: Optional per-request timeout.

        Returns:
            Arbitrary result from the first successful candidate runnable.

        Raises:
            BaseException: Re-raises the last candidate initialization or
                invocation failure when all candidates fail.
        """
        call_timeout = timeout_seconds or timeout_for_task_name(task.value)
        # Outer asyncio guard — slightly longer than per-request HTTP timeout
        outer_timeout = call_timeout + _TIMEOUT_SLACK_SECONDS
        last_error: Optional[BaseException] = None
        candidates = self.candidate_models(task)
        run_config = config or {}

        for model_ref in candidates:
            try:
                llm = self.get_llm(
                    task,
                    temperature=temperature,
                    model_name=model_ref,
                    timeout_seconds=call_timeout,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Cannot initialize model '%s' for task '%s': %s",
                    model_ref,
                    task.value,
                    exc,
                )
                continue

            chain = build_chain(llm)
            try:
                logger.info(
                    "Invoking task '%s' with model '%s' (timeout=%ss)",
                    task.value,
                    model_ref,
                    call_timeout,
                )

                def _sync_invoke(bound_chain=chain, bound_inputs=inputs, bound_config=run_config):
                    """Invoke the bound chain in the worker thread."""
                    return invoke_llm_sync(bound_chain, bound_inputs, bound_config)

                return await asyncio.wait_for(
                    asyncio.to_thread(_sync_invoke),
                    timeout=outer_timeout,
                )
            except Exception as exc:
                last_error = exc
                if _is_retriable_llm_error(exc) and model_ref != candidates[-1]:
                    logger.warning(
                        "Model '%s' failed for task '%s' (%s). Falling over.",
                        model_ref,
                        task.value,
                        exc,
                    )
                    continue
                if model_ref == candidates[-1]:
                    break
                logger.warning(
                    "Model '%s' failed for task '%s' (%s). Trying next.",
                    model_ref,
                    task.value,
                    exc,
                )

        assert last_error is not None
        raise last_error

    def invoke_with_failover_sync(
        self,
        task: TaskType,
        build_chain: Callable[[BaseChatModel], Any],
        inputs: Any,
        config: Optional[Dict[str, Any]] = None,
        temperature: float = 0.1,
        timeout_seconds: Optional[float] = None,
    ) -> Any:
        """Invoke synchronously with ordered model failover.

        Args:
            task: Pipeline task category.
            build_chain: Callback that binds a model into a runnable.
            inputs: Arbitrary runnable input.
            config: Optional LangChain run configuration.
            temperature: Sampling temperature.
            timeout_seconds: Optional request timeout.

        Returns:
            Arbitrary result from the first successful model.

        Raises:
            BaseException: Re-raises the final failure when no candidate works.
        """
        call_timeout = timeout_seconds or timeout_for_task_name(task.value)
        last_error: Optional[BaseException] = None
        candidates = self.candidate_models(task)
        run_config = config or {}

        for model_ref in candidates:
            try:
                llm = self.get_llm(
                    task,
                    temperature=temperature,
                    model_name=model_ref,
                    timeout_seconds=call_timeout,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Cannot initialize model '%s' for task '%s': %s",
                    model_ref,
                    task.value,
                    exc,
                )
                continue

            chain = build_chain(llm)
            try:
                logger.info(
                    "Sync invoke task '%s' model '%s' (timeout=%ss)",
                    task.value,
                    model_ref,
                    call_timeout,
                )
                return invoke_llm_sync(chain, inputs, run_config)
            except Exception as exc:
                last_error = exc
                if _is_retriable_llm_error(exc) and model_ref != candidates[-1]:
                    logger.warning(
                        "Model '%s' failed (%s). Falling over.", model_ref, exc
                    )
                    continue
                if model_ref == candidates[-1]:
                    break

        assert last_error is not None
        raise last_error

    def routing_summary(self) -> Dict[str, str]:
        """Return selected primary model references keyed by task value."""
        return {task.value: self.select_model(task) for task in TaskType}


_router: Optional[ModelRouter] = None


def get_model_router() -> ModelRouter:
    """Return the process-wide lazily initialized model router."""
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def reset_model_router() -> None:
    """Clear the process-wide router so configuration is reloaded next use."""
    global _router
    _router = None
