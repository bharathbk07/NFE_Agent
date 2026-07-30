"""Knowledge-first IR reuse and JSON structured-output hardening."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.utils.json_parsing import parse_json_from_llm
from src.utils.script_recipes import prefer_prior_ir
from src.utils.script_reuse import try_load_prior_ir_for_state


def test_parse_json_empty_fence_raises_value_error():
    with pytest.raises(ValueError, match="empty"):
        parse_json_from_llm("```json\n```")


def test_parse_json_prose_without_object_raises():
    with pytest.raises(ValueError):
        parse_json_from_llm("sorry I cannot help with that")


def test_prefer_prior_ir_for_reuse_and_jira():
    assert prefer_prior_ir({"recording_mode": "reuse"}) is True
    assert prefer_prior_ir({"skip_k6_smoke": True, "recording_file": "/tmp/x.json"}) is True
    assert prefer_prior_ir({"force_rebuild_ir": True, "recording_mode": "reuse"}) is False
    assert prefer_prior_ir({}) is False


def test_try_load_prior_ir_finds_saved_file(tmp_path, monkeypatch):
    app = "example.com"
    flow = "create-claim"
    ir = {
        "version": 1,
        "transactions": [{"name": "Login", "mode": "protocol", "requests": []}],
        "vars": [{"name": "username"}],
        "correlations": [{"name": "csrf", "var_name": "csrf"}],
    }
    app_dir = tmp_path / app
    app_dir.mkdir(parents=True)
    (app_dir / f"{flow}_ir.json").write_text(json.dumps(ir), encoding="utf-8")

    monkeypatch.setenv("NFE_ARTIFACTS_DIR", str(tmp_path))
    # Reload artifacts_dir resolution
    from src.utils import artifacts as art

    monkeypatch.setattr(art, "artifacts_dir", lambda: Path(tmp_path))

    state = {
        "recording_mode": "reuse",
        "target_url": f"https://{app}/login",
        "app": app,
        "flow": flow,
    }
    loaded = try_load_prior_ir_for_state(state)
    assert loaded is not None
    assert loaded.get("meta", {}).get("reused_prior_ir") is True
    assert len(loaded.get("transactions") or []) == 1
