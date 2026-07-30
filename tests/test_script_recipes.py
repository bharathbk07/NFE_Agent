"""Tests for per-app/flow script recipe knowledge reuse."""

from __future__ import annotations

from src.utils.script_recipes import (
    apply_script_recipe_to_ir,
    has_green_recipe,
    read_script_recipe,
    upsert_script_recipe,
)


def test_recipe_roundtrip_and_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.utils.script_recipes.artifacts_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "src.utils.script_recipes.ensure_app_dirs", lambda _app: None
    )

    app = "opensource-demo.orangehrmlive.com"
    flow = "create-claim"
    ir = {
        "correlations": [
            {
                "name": "csrf_token",
                "var_name": "csrf_token",
                "extract_how": "regex",
                "pass_to": "login",
            }
        ],
        "vars": [{"name": "csrf_token"}],
        "transactions": [],
    }
    path = upsert_script_recipe(
        app,
        flow,
        ir=ir,
        heal_notes=["wired csrf from smoke"],
        smoke_ok=True,
        target_url="https://opensource-demo.orangehrmlive.com/",
    )
    assert path is not None
    assert has_green_recipe(app, flow)
    stored = read_script_recipe(app, flow)
    assert stored and stored["smoke_ok"] is True
    assert stored["correlations"]

    fresh = {"correlations": [], "vars": [], "transactions": []}
    merged, notes = apply_script_recipe_to_ir(fresh, app, flow)
    assert any("csrf" in str(c.get("name") or c.get("var_name") or "") for c in merged["correlations"])
    assert notes
    assert (merged.get("meta") or {}).get("recipe_applied") is True


def test_failed_recipe_not_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "src.utils.script_recipes.artifacts_root", lambda: tmp_path
    )
    monkeypatch.setattr(
        "src.utils.script_recipes.ensure_app_dirs", lambda _app: None
    )
    upsert_script_recipe(
        "app.example",
        "flow",
        ir={"correlations": [{"name": "x", "var_name": "x"}]},
        smoke_ok=False,
    )
    assert not has_green_recipe("app.example", "flow")
    ir, notes = apply_script_recipe_to_ir(
        {"correlations": [], "vars": []}, "app.example", "flow"
    )
    assert ir["correlations"] == []
    assert notes == []
