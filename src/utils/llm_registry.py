"""
Multi-provider LLM registry: Google Gemini + Cursor (native SDK).

Model references in LLM_MODELS / LLM_TASK_ROUTING:
  gemini-3.5-flash              → google
  google:gemini-3.5-flash
  gpt-5.5                       → cursor (when CURSOR_API_KEY set)
  cursor:composer-2.5
  claude-sonnet-4               → cursor (NOT sent to Gemini)

Cursor models use the native cursor-sdk (Agent.prompt) — no local proxy.
They are only used for orchestration and navigation; extraction/self_heal
stay on Google Gemini.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
REASONING_TASK_TIMEOUT = float(
    os.getenv("LLM_TIMEOUT_REASONING", os.getenv("LLM_TIMEOUT_SECONDS", "120"))
)
DEFAULT_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))

# Model id hints that must NOT be sent to Google Gemini
CURSOR_MODEL_HINTS = (
    "composer",
    "cursor-",
    "gpt-",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude",
    "sonnet",
    "opus",
    "grok",
    "llama",
    "deepseek",
)


@dataclass(frozen=True)
class ModelSpec:
    """Resolved provider and model reference from environment configuration."""

    provider: str  # google | cursor
    model_id: str
    ref: str  # provider:model_id

    @staticmethod
    def parse(raw: str) -> "ModelSpec":
        """Parse a prefixed or inferred model reference.

        Args:
            raw: Model ID or ``provider:model_id`` reference.

        Returns:
            Normalized immutable model specification.

        Raises:
            ValueError: If the reference is empty or names an unknown provider.
        """
        text = (raw or "").strip()
        if not text:
            raise ValueError("Empty model reference")

        if ":" in text:
            provider, model_id = text.split(":", 1)
            provider = provider.strip().lower()
            model_id = model_id.strip()
        else:
            model_id = text
            provider = _infer_provider(model_id)

        if provider not in ("google", "cursor"):
            raise ValueError(f"Unknown provider '{provider}' in model ref '{raw}'")

        return ModelSpec(provider=provider, model_id=model_id, ref=f"{provider}:{model_id}")

    def display_name(self) -> str:
        """Return the normalized ``provider:model_id`` display string."""
        return self.ref


def _infer_provider(model_id: str) -> str:
    """Infer the API provider from model-name hints and credentials.

    Args:
        model_id: Unprefixed model identifier.

    Returns:
        ``google`` or ``cursor`` provider name.
    """
    lower = model_id.lower().strip()

    if lower.startswith("gemini") or lower.startswith("models/gemini") or "gemini" in lower:
        return "google"

    if any(h in lower for h in CURSOR_MODEL_HINTS):
        return "cursor"

    if settings.CURSOR_API_KEY:
        logger.info(
            "Model %r has no provider prefix; using cursor SDK.", model_id
        )
        return "cursor"

    logger.warning(
        "Model %r is not a Gemini model. Prefix with cursor: or google: explicitly.",
        model_id,
    )
    return "google"


def is_cursor_sdk_viable() -> bool:
    """Check whether native Cursor SDK configuration is complete.

    Returns:
        ``True`` when an API key and any cloud repository requirement exist.
    """
    if not settings.CURSOR_API_KEY:
        return False
    runtime = (settings.CURSOR_RUNTIME or "local").strip().lower()
    if runtime == "cloud":
        return bool(settings.CURSOR_CLOUD_REPO)
    return True


def is_viable_spec(spec: ModelSpec) -> bool:
    """Check whether credentials permit use of a model specification.

    Args:
        spec: Resolved provider/model specification.

    Returns:
        ``True`` when the selected provider is configured.
    """
    if spec.provider == "google":
        return bool(settings.GEMINI_API_KEY)
    if spec.provider == "cursor":
        return is_cursor_sdk_viable()
    return False


def parse_model_list(raw_models: Optional[List[str]] = None) -> List[ModelSpec]:
    """Parse and deduplicate viable configured model references.

    Args:
        raw_models: Optional references; defaults to configured available models.

    Returns:
        Ordered list of unique, credential-backed model specifications.
    """
    if raw_models is None:
        raw_models = settings.available_models

    specs: List[ModelSpec] = []
    seen: set[str] = set()
    for raw in raw_models:
        try:
            spec = ModelSpec.parse(raw)
        except ValueError as exc:
            logger.warning("Skipping invalid model entry %r: %s", raw, exc)
            continue
        if spec.ref in seen:
            continue
        if not is_viable_spec(spec):
            logger.warning(
                "Skipping %s — missing credentials for provider '%s'.",
                spec.ref,
                spec.provider,
            )
            continue
        seen.add(spec.ref)
        specs.append(spec)

    return specs


def timeout_for_task_name(task_key: str) -> float:
    """Choose a request timeout for a task category.

    Args:
        task_key: Task name such as ``orchestration`` or ``extraction``.

    Returns:
        Timeout duration in seconds.
    """
    if task_key in ("orchestration", "navigation"):
        return REASONING_TASK_TIMEOUT
    return DEFAULT_LLM_TIMEOUT_SECONDS


def capability_score(spec: ModelSpec) -> int:
    """Estimate relative model capability for deterministic routing.

    Args:
        spec: Resolved model specification.

    Returns:
        Integer score where larger values indicate stronger, typically slower,
        reasoning capability.
    """
    n = spec.model_id.lower()

    if spec.provider == "cursor":
        if any(x in n for x in ("gpt-5", "o3", "o4", "opus")):
            return 62
        if "claude" in n and ("4-5" in n or "4.5" in n):
            return 58
        if "claude" in n or "sonnet" in n:
            return 54
        if "grok" in n:
            return 50
        if "composer-2.5" in n or "composer-2-5" in n:
            return 48
        if "composer" in n:
            return 42
        if "gpt" in n:
            return 46
        return 40

    if "lite" in n:
        score = 10
    elif "pro" in n or "ultra" in n:
        score = 55
    elif "thinking" in n:
        score = 58
    elif "flash" in n:
        score = 32
    else:
        score = 25

    if re.search(r"3\.5", n):
        score += 6
    elif re.search(r"3\.1", n):
        score += 2

    return score


def is_fast_model(spec: ModelSpec) -> bool:
    """Classify a model as suitable for low-latency tasks.

    Args:
        spec: Resolved model specification.

    Returns:
        ``True`` for low-scoring Google models; Cursor models are never fast.
    """
    if spec.provider == "cursor":
        return False
    n = spec.model_id.lower()
    return "lite" in n or capability_score(spec) <= 15


def is_reasoning_model(spec: ModelSpec) -> bool:
    """Classify a model as suitable for reasoning-heavy tasks.

    Args:
        spec: Resolved model specification.

    Returns:
        Boolean reasoning classification derived from provider and model name.
    """
    if spec.provider == "cursor":
        return True
    n = spec.model_id.lower()
    return any(tag in n for tag in ("pro", "ultra", "thinking")) or (
        "flash" in n and "lite" not in n and capability_score(spec) >= 30
    )


def build_chat_model(
    spec: ModelSpec,
    *,
    temperature: float = 0.1,
    timeout: Optional[float] = None,
) -> BaseChatModel:
    """Instantiate a provider-specific LangChain chat model.

    Args:
        spec: Provider and model identifier to instantiate.
        temperature: Sampling temperature.
        timeout: Optional request timeout in seconds.

    Returns:
        Google or Cursor-backed ``BaseChatModel``.

    Raises:
        ValueError: If credentials or provider support are missing.
        ImportError: If Cursor SDK support is requested but unavailable.
    """
    call_timeout = timeout if timeout is not None else DEFAULT_LLM_TIMEOUT_SECONDS

    if spec.provider == "google":
        if not settings.GEMINI_API_KEY:
            raise ValueError(f"GEMINI_API_KEY is required for '{spec.ref}'.")
        return ChatGoogleGenerativeAI(
            model=spec.model_id,
            google_api_key=settings.GEMINI_API_KEY,
            temperature=temperature,
            max_retries=DEFAULT_MAX_RETRIES,
            timeout=call_timeout,
            disable_streaming=True,
        )

    if spec.provider == "cursor":
        if not settings.CURSOR_API_KEY:
            raise ValueError(f"CURSOR_API_KEY is required for '{spec.ref}'.")
        try:
            from src.utils.cursor_sdk_llm import build_cursor_sdk_chat_model
        except ImportError as exc:
            raise ImportError(
                "cursor-sdk is required for cursor models. pip install cursor-sdk"
            ) from exc

        return build_cursor_sdk_chat_model(
            model_id=spec.model_id,
            api_key=settings.CURSOR_API_KEY,
            timeout=call_timeout,
            runtime=settings.CURSOR_RUNTIME,
            cloud_repo=settings.CURSOR_CLOUD_REPO or None,
            workdir=settings.CURSOR_WORKDIR or None,
        )

    raise ValueError(f"Unsupported provider '{spec.provider}'")
