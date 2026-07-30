"""Cursor SDK LLM registry (sole LLM provider).

Model refs in LLM_MODELS / CURSOR_DEFAULT_MODEL:
  cursor:composer-2.5
  composer-2.5          → cursor (inferred)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import List, Optional

from langchain_core.language_models.chat_models import BaseChatModel

from config.settings import settings

logger = logging.getLogger(__name__)

DEFAULT_LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
REASONING_TASK_TIMEOUT = float(
    os.getenv("LLM_TIMEOUT_REASONING", os.getenv("LLM_TIMEOUT_SECONDS", "120"))
)
DEFAULT_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "1"))


@dataclass(frozen=True)
class ModelSpec:
    """Resolved provider and model reference from environment configuration."""

    provider: str  # cursor
    model_id: str
    ref: str  # provider:model_id

    @staticmethod
    def parse(raw: str) -> "ModelSpec":
        text = (raw or "").strip()
        if not text:
            raise ValueError("Empty model reference")

        if ":" in text:
            provider, model_id = text.split(":", 1)
            provider = provider.strip().lower()
            model_id = model_id.strip()
        else:
            model_id = text
            provider = "cursor"

        if provider == "google" or model_id.lower().startswith("gemini"):
            raise ValueError(
                f"Google/Gemini models are not supported ({raw!r}). "
                "Use cursor:composer-2.5 (or another Cursor model id)."
            )
        if provider != "cursor":
            raise ValueError(
                f"Unknown provider '{provider}' in model ref '{raw}'. "
                "Only cursor:… is supported."
            )
        return ModelSpec(provider="cursor", model_id=model_id, ref=f"cursor:{model_id}")

    def display_name(self) -> str:
        return self.ref


def is_cursor_sdk_viable() -> bool:
    if not settings.CURSOR_API_KEY:
        return False
    runtime = (settings.CURSOR_RUNTIME or "local").strip().lower()
    if runtime == "cloud":
        return bool(settings.CURSOR_CLOUD_REPO)
    return True


def is_viable_spec(spec: ModelSpec) -> bool:
    return spec.provider == "cursor" and is_cursor_sdk_viable()


def parse_model_list(raw_models: Optional[List[str]] = None) -> List[ModelSpec]:
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
                "Skipping %s — set CURSOR_API_KEY (and CURSOR_CLOUD_REPO if cloud).",
                spec.ref,
            )
            continue
        seen.add(spec.ref)
        specs.append(spec)
    return specs


def timeout_for_task_name(task_key: str) -> float:
    if task_key in ("orchestration", "navigation", "assist"):
        return REASONING_TASK_TIMEOUT
    return DEFAULT_LLM_TIMEOUT_SECONDS


def capability_score(spec: ModelSpec) -> int:
    n = spec.model_id.lower()
    if any(x in n for x in ("gpt-5", "o3", "o4", "opus")):
        return 62
    if "claude" in n and ("4-5" in n or "4.5" in n):
        return 58
    if "claude" in n or "sonnet" in n:
        return 54
    if "composer-2.5" in n or "composer-2-5" in n:
        return 48
    if "composer" in n:
        return 42
    if "gpt" in n:
        return 46
    return 40


def is_fast_model(spec: ModelSpec) -> bool:
    """Cursor SDK is one-shot; treat all as non-fast for ranking."""
    del spec
    return False


def is_reasoning_model(spec: ModelSpec) -> bool:
    del spec
    return True


def build_chat_model(
    spec: ModelSpec,
    *,
    temperature: float = 0.1,
    timeout: Optional[float] = None,
) -> BaseChatModel:
    del temperature  # Cursor SDK path ignores sampling temperature
    call_timeout = timeout if timeout is not None else DEFAULT_LLM_TIMEOUT_SECONDS

    if spec.provider != "cursor":
        raise ValueError(f"Unsupported provider '{spec.provider}' (cursor only)")
    if not settings.CURSOR_API_KEY:
        raise ValueError(f"CURSOR_API_KEY is required for '{spec.ref}'.")

    from src.utils.cursor_sdk_llm import build_cursor_sdk_chat_model

    return build_cursor_sdk_chat_model(
        model_id=spec.model_id,
        api_key=settings.CURSOR_API_KEY,
        timeout=call_timeout,
        runtime=settings.CURSOR_RUNTIME,
        cloud_repo=settings.CURSOR_CLOUD_REPO or None,
        workdir=settings.CURSOR_WORKDIR or None,
    )
