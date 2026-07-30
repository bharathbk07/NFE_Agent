"""Knowledge-first reuse of saved Load-Test IR / recipes for the same recording."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def resolve_app_flow_from_state(state: Dict[str, Any]) -> Tuple[str, str]:
    app_id = str(state.get("app") or "")
    flow_id = str(state.get("flow") or state.get("recording_label") or "")
    try:
        from src.utils.app_registry import resolve_app_and_flow

        app_id, flow_id = resolve_app_and_flow(
            target_url=state.get("target_url") or "",
            label=flow_id,
            recording_hint=str(state.get("recording_file") or flow_id or ""),
            explicit_app=app_id,
        )
    except Exception:
        pass
    return app_id or "", flow_id or "default"


def try_load_prior_ir_for_state(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a reusable saved IR when knowledge-first reuse applies."""
    from src.utils.artifacts import load_load_test_ir
    from src.utils.script_recipes import has_green_recipe, prefer_prior_ir

    if not prefer_prior_ir(state):
        return None

    app_id, flow_id = resolve_app_flow_from_state(state)
    prior = load_load_test_ir(
        target_url=state.get("target_url") or "",
        app=app_id,
        flow=flow_id,
        label=str(state.get("recording_label") or ""),
    )
    if not prior or not (prior.get("transactions") or []):
        logger.info(
            "No reusable prior IR for %s/%s — full analyse from recording",
            app_id,
            flow_id,
        )
        return None

    txns = len(prior.get("transactions") or [])
    has_recipe = has_green_recipe(app_id, flow_id)
    logger.info(
        "Knowledge-first reuse: loaded prior IR for %s/%s "
        "(transactions=%s green_recipe=%s)",
        app_id,
        flow_id,
        txns,
        has_recipe,
    )
    meta = dict(prior.get("meta") or {})
    meta["reused_prior_ir"] = True
    meta["reuse_app"] = app_id
    meta["reuse_flow"] = flow_id
    prior = dict(prior)
    prior["meta"] = meta
    return prior


def materialize_k6_from_prior_ir(
    state: Dict[str, Any],
    prior_ir: Dict[str, Any],
) -> Tuple[Dict[str, Any], str, List[str], str, str]:
    """Apply recipe + emit k6 from a prior IR.

    Returns ``(ir, k6_script, notes, app_id, flow_id)``.
    """
    from src.utils.k6_generator import emit_k6_from_ir, generate_k6_script
    from src.utils.script_recipes import apply_script_recipe_to_ir

    app_id, flow_id = resolve_app_flow_from_state(state)
    notes: List[str] = [
        f"**Knowledge reuse:** loaded saved IR for `{app_id}/{flow_id}` "
        "instead of rebuilding correlations from scratch."
    ]
    ir, recipe_notes = apply_script_recipe_to_ir(
        dict(prior_ir), app_id, flow_id or "default"
    )
    notes.extend(recipe_notes)

    k6_script = emit_k6_from_ir(ir) or ""
    if not k6_script:
        k6_script = generate_k6_script(
            target_url=state.get("target_url") or "",
            parameterizable_candidates=list(ir.get("vars") or []),
            dependencies=list(ir.get("dependencies") or []),
            transactions=list(ir.get("transactions") or []),
            network_requests=[],
            ir=ir,
        )
    return ir, k6_script, notes, app_id, flow_id
