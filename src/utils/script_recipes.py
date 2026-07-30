"""Per-app / per-flow script recipes — reusable correlation + heal knowledge.

Long-term shape: each application domain stores flow recipes under
``artifacts/knowledge/<app>/flows/<flow>_recipe.json``.

Bootstrap: capture + analyse + heal once → persist recipe when smoke passes.
Reuse: next runs merge recipe into IR **before** smoke, and skip optional Run 3
when a green recipe already exists for that app/flow.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.security.fs_jail import assert_under_jail
from src.utils.app_registry import artifacts_root, ensure_app_dirs, slug_flow

logger = logging.getLogger(__name__)

RECIPE_VERSION = 1


def recipe_path(app_id: str, flow_id: str) -> Path:
    app = (app_id or "").strip()
    flow = slug_flow(flow_id) or (flow_id or "default").strip() or "default"
    ensure_app_dirs(app)
    root = artifacts_root() / "knowledge"
    path = root / app / "flows" / f"{flow}_recipe.json"
    return assert_under_jail(path, root)


def read_script_recipe(app_id: str, flow_id: str) -> Optional[Dict[str, Any]]:
    """Load a recipe dict, or None if missing/invalid."""
    if not (app_id or "").strip():
        return None
    try:
        path = recipe_path(app_id, flow_id)
    except Exception:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Failed to read script recipe %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    return data


def has_green_recipe(app_id: str, flow_id: str) -> bool:
    recipe = read_script_recipe(app_id, flow_id)
    return bool(recipe and recipe.get("smoke_ok") is True)


def prefer_prior_ir(state: Dict[str, Any]) -> bool:
    """Whether this turn should load a saved IR before rebuilding from traffic."""
    if state.get("force_rebuild_ir"):
        return False
    if state.get("prefer_prior_ir"):
        return True
    if (state.get("recording_mode") or "") == "reuse":
        return True
    # Jira worker always starts from a saved recording
    if state.get("skip_k6_smoke") and state.get("recording_file"):
        return True
    return False


def upsert_script_recipe(
    app_id: str,
    flow_id: str,
    *,
    ir: Optional[Dict[str, Any]] = None,
    heal_notes: Optional[List[str]] = None,
    smoke_ok: bool = False,
    target_url: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Optional[Path]:
    """Persist a recipe after a smoke attempt (prefer calling on pass)."""
    app = (app_id or "").strip()
    flow = slug_flow(flow_id) or (flow_id or "default").strip() or "default"
    if not app:
        return None

    ir = ir or {}
    correlations = list(ir.get("correlations") or [])
    # Keep a compact, reusable subset
    compact_corrs: List[Dict[str, Any]] = []
    for c in correlations[:40]:
        if not isinstance(c, dict):
            continue
        compact = {
            k: c.get(k)
            for k in (
                "name",
                "extract_from",
                "extract_how",
                "extract_expr",
                "pass_to",
                "pass_as",
                "var_name",
                "cookie_name",
                "source_url",
                "target_url",
            )
            if c.get(k) is not None
        }
        if compact:
            compact_corrs.append(compact)

    vars_compact = []
    for v in list(ir.get("vars") or [])[:40]:
        if isinstance(v, dict):
            vars_compact.append(
                {k: v.get(k) for k in ("name", "source", "default", "role") if v.get(k) is not None}
            )
        elif isinstance(v, str):
            vars_compact.append({"name": v})

    recipe = {
        "version": RECIPE_VERSION,
        "app": app,
        "flow": flow,
        "target_url": target_url or "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "smoke_ok": bool(smoke_ok),
        "heal_notes": [str(n) for n in (heal_notes or []) if n][:40],
        "correlations": compact_corrs,
        "vars": vars_compact,
        "auth": {
            "has_csrf": any(
                "csrf" in str(c.get("name") or c.get("var_name") or "").lower()
                or "token" in str(c.get("name") or c.get("var_name") or "").lower()
                for c in compact_corrs
            ),
            "cookie_correlations": sum(
                1
                for c in compact_corrs
                if str(c.get("extract_how") or "").lower() in {"cookie", "set-cookie"}
                or c.get("cookie_name")
            ),
        },
        "extra": dict(extra or {}),
    }

    try:
        path = recipe_path(app, flow)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        logger.info(
            "Script recipe upserted app=%s flow=%s smoke_ok=%s path=%s",
            app,
            flow,
            smoke_ok,
            path,
        )
        return path
    except Exception as exc:
        logger.warning("Script recipe upsert failed: %s", exc)
        return None


def _corr_key(c: Dict[str, Any]) -> str:
    return "|".join(
        str(c.get(k) or "")
        for k in ("name", "var_name", "extract_from", "pass_to", "cookie_name")
    )


def apply_script_recipe_to_ir(
    ir: Dict[str, Any],
    app_id: str,
    flow_id: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Merge a known-green recipe into IR before smoke.

    Returns (ir, notes). No-op when no recipe or recipe never passed smoke.
    """
    notes: List[str] = []
    recipe = read_script_recipe(app_id, flow_id)
    if not recipe or recipe.get("smoke_ok") is not True:
        return ir, notes

    healed = dict(ir)
    healed["transactions"] = [dict(t) for t in (ir.get("transactions") or [])]
    healed["correlations"] = list(ir.get("correlations") or [])
    healed["vars"] = list(ir.get("vars") or [])

    existing = {_corr_key(c) for c in healed["correlations"] if isinstance(c, dict)}
    added = 0
    for c in recipe.get("correlations") or []:
        if not isinstance(c, dict):
            continue
        key = _corr_key(c)
        if key and key not in existing:
            healed["correlations"].append(dict(c))
            existing.add(key)
            added += 1

    existing_vars = set()
    for v in healed["vars"]:
        if isinstance(v, dict):
            existing_vars.add(str(v.get("name") or ""))
        else:
            existing_vars.add(str(v))
    var_added = 0
    for v in recipe.get("vars") or []:
        name = v.get("name") if isinstance(v, dict) else str(v)
        if name and name not in existing_vars:
            healed["vars"].append(dict(v) if isinstance(v, dict) else {"name": name})
            existing_vars.add(str(name))
            var_added += 1

    if added or var_added:
        notes.append(
            f"Applied known script recipe for `{app_id}/{flow_id}` "
            f"(+{added} correlations, +{var_added} vars from prior green smoke)."
        )
        prior = list(recipe.get("heal_notes") or [])[:5]
        if prior:
            notes.append("Prior heal notes: " + "; ".join(prior))
    else:
        notes.append(
            f"Known script recipe present for `{app_id}/{flow_id}` "
            "(IR already contained matching correlations)."
        )

    # Tag IR so reports show knowledge reuse
    meta = dict(healed.get("meta") or {})
    meta["recipe_applied"] = True
    meta["recipe_app"] = app_id
    meta["recipe_flow"] = flow_id
    healed["meta"] = meta
    return healed, notes
