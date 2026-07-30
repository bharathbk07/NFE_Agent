"""Cursor-only model eligibility."""

from __future__ import annotations

import pytest

from src.utils.llm_registry import ModelSpec
from src.utils.model_router import TaskType, _specs_for_task, reset_model_router


def test_cursor_model_parse():
    spec = ModelSpec.parse("composer-2.5")
    assert spec.provider == "cursor"
    assert spec.ref == "cursor:composer-2.5"


def test_gemini_rejected():
    with pytest.raises(ValueError, match="not supported"):
        ModelSpec.parse("google:gemini-2.0-flash")
    with pytest.raises(ValueError, match="not supported"):
        ModelSpec.parse("gemini-2.0-flash")


def test_cursor_eligible_all_tasks():
    specs = [ModelSpec.parse("cursor:composer-2.5")]
    for task in TaskType:
        assert _specs_for_task(specs, task) == specs


def test_router_cursor_only(monkeypatch):
    from src.utils import model_router as mr

    monkeypatch.setattr(
        mr,
        "parse_model_list",
        lambda: [ModelSpec.parse("cursor:composer-2.5")],
    )
    reset_model_router()
    router = mr.ModelRouter()
    assert router.select_model(TaskType.EXTRACTION) == "cursor:composer-2.5"
    assert router.select_model(TaskType.ASSIST) == "cursor:composer-2.5"
    reset_model_router()
