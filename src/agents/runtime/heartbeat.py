"""Heartbeat — optional autonomous wake without a user message (OpenClaw-style)."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from src.agents.runtime.memory import append_note
from src.agents.runtime.skills import load_skill

logger = logging.getLogger(__name__)

HEARTBEAT_OK = "HEARTBEAT_OK"


def _list_eligible_stories() -> Dict[str, Any]:
    from src.tools.pe_assistant_tools import _list_jira_stories_impl

    raw = _list_jira_stories_impl(assist_fallback=True)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"markdown": raw, "keys": []}
    return data if isinstance(data, dict) else {"keys": [], "markdown": str(data)}


def _unfinished_jobs_hint() -> List[Dict[str, Any]]:
    """Best-effort peek at on-disk job status files if present."""
    try:
        from pathlib import Path

        from src.utils.app_registry import artifacts_root

        jobs_dir = artifacts_root() / "api" / "jobs"
        if not jobs_dir.is_dir():
            return []
        out: List[Dict[str, Any]] = []
        for path in sorted(jobs_dir.glob("*.json"), reverse=True)[:10]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = str(data.get("status") or "").lower()
            if status in {"failed", "error", "running", "queued"}:
                out.append(
                    {
                        "id": data.get("id") or path.stem,
                        "status": status,
                        "title": data.get("title") or data.get("kind"),
                    }
                )
        return out
    except Exception:
        return []


async def run_heartbeat_once(
    *,
    thread_id: str = "heartbeat",
    propose_only: bool = True,
) -> Dict[str, Any]:
    """One heartbeat tick.

    Cheap path: enumerate eligible work. Escalate to ASSIST only when acting
    (propose_only=False and policy allows — still defaults to propose).
    """
    from config.settings import settings

    if not getattr(settings, "NFE_HEARTBEAT_ENABLED", False):
        return {
            "status": "disabled",
            "message": HEARTBEAT_OK,
            "reason": "NFE_HEARTBEAT_ENABLED is false",
        }

    checklist = load_skill("HEARTBEAT")
    stories = _list_eligible_stories()
    keys = list(stories.get("keys") or [])
    jobs = _unfinished_jobs_hint()

    actionable: List[str] = []
    if keys:
        actionable.append(f"Eligible Jira stories: {', '.join(keys[:8])}")
    if jobs:
        actionable.append(
            "Jobs needing attention: "
            + ", ".join(f"{j.get('id')}({j.get('status')})" for j in jobs[:5])
        )

    if not actionable:
        append_note(thread_id, HEARTBEAT_OK, kind="heartbeat")
        return {
            "status": "ok",
            "message": HEARTBEAT_OK,
            "checklist_loaded": bool(checklist and not checklist.startswith("Unknown")),
            "stories": keys,
            "jobs": jobs,
        }

    proposal = (
        "Heartbeat found work:\n- "
        + "\n- ".join(actionable)
        + "\n\nPropose (do not auto-execute unless policy explicitly allows): "
        "rank stories, then ask human or wait for Studio/Console authorization."
    )
    append_note(thread_id, proposal[:500], kind="heartbeat")

    if propose_only:
        return {
            "status": "propose",
            "message": proposal,
            "stories": keys,
            "jobs": jobs,
            "executed": False,
        }

    # Escalation: ask ASSIST Brain for a short next-step plan (no silent execute)
    from langchain_core.messages import HumanMessage, SystemMessage

    from src.utils.model_router import TaskType, get_model_router

    router = get_model_router()
    final = await router.ainvoke_with_failover(
        TaskType.ASSIST,
        lambda model: model,
        [
            SystemMessage(
                content=(
                    "You are the NFE PE heartbeat. Given eligible work, propose "
                    "ONE next PE action. Do not claim you executed anything. "
                    "Keep under 120 words."
                )
            ),
            HumanMessage(content=proposal),
        ],
    )
    text = getattr(final, "content", None) or str(final)
    if isinstance(text, list):
        text = " ".join(
            str(b.get("text") if isinstance(b, dict) else b) for b in text
        )
    return {
        "status": "propose",
        "message": str(text),
        "stories": keys,
        "jobs": jobs,
        "executed": False,
        "escalated": True,
    }


async def heartbeat_loop(
    *,
    interval_sec: Optional[int] = None,
    max_ticks: Optional[int] = None,
) -> None:
    from config.settings import settings

    interval = int(
        interval_sec
        if interval_sec is not None
        else getattr(settings, "NFE_HEARTBEAT_INTERVAL_SEC", 300) or 300
    )
    ticks = 0
    while True:
        result = await run_heartbeat_once()
        logger.info("heartbeat tick=%s result=%s", ticks, result.get("status"))
        print(json.dumps(result, default=str)[:2000])
        ticks += 1
        if max_ticks is not None and ticks >= max_ticks:
            break
        await asyncio.sleep(max(30, interval))


def main() -> None:
    parser = argparse.ArgumentParser(description="NFE PE Agent heartbeat")
    parser.add_argument("--once", action="store_true", help="Single tick then exit")
    parser.add_argument("--escalate", action="store_true", help="Use ASSIST for propose text")
    parser.add_argument("--interval", type=int, default=None)
    parser.add_argument("--max-ticks", type=int, default=None)
    args = parser.parse_args()

    async def _run() -> None:
        if args.once:
            out = await run_heartbeat_once(propose_only=not args.escalate)
            print(json.dumps(out, indent=2, default=str))
            return
        await heartbeat_loop(interval_sec=args.interval, max_ticks=args.max_ticks)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
