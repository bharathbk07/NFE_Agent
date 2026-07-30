"""PE Hands implementations — bridges to existing domain workers."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.agents.runtime.hands_registry import (
    AppFlowArgs,
    CreateIssueArgs,
    EmptyArgs,
    ExecuteStoryArgs,
    HandsRegistry,
    HandSpec,
    IssueKeyArgs,
    JiraCommentArgs,
    LoadSkillArgs,
    QueryArgs,
    RankStoriesArgs,
    ReuseRecordingArgs,
    RiskTier,
    SearchKnowledgeArgs,
    SmokeArgs,
    TrendsArgs,
    WatchMeArgs,
    make_tool,
)
from src.security.secrets import redact_text_for_llm

logger = logging.getLogger(__name__)


def asyncio_run_safe(coro):
    """Run coroutine from sync context without breaking an active loop."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Nested: create task is not possible from sync; use a thread
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ok(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str)[:12000]


def build_default_hands(*, state: Optional[Dict[str, Any]] = None) -> HandsRegistry:
    """Register all PE Hands for one agent turn."""
    state = state or {}
    reg = HandsRegistry()

    # --- Skills ---
    def _load_skill(skill_id: str) -> str:
        from src.agents.runtime.skills import load_skill

        body = load_skill(skill_id)
        return _ok({"skill_id": skill_id, "markdown": body, "source": "hand:load_skill"})

    reg.register(
        HandSpec(
            name="load_skill",
            description="Load a PE Skill playbook by id (from the skill catalog).",
            risk=RiskTier.READ,
            capability="skills",
            tool=make_tool(
                name="load_skill",
                description="Load full PE Skill markdown by id.",
                func=_load_skill,
                args_schema=LoadSkillArgs,
            ),
        )
    )

    # --- Knowledge / trends ---
    def _search_knowledge(query: str, app: str = "", flow: str = "") -> str:
        from src.tools.pe_assistant_tools import _search_knowledge_impl

        app = app or str(state.get("app") or "")
        flow = flow or str(state.get("flow") or "")
        return _search_knowledge_impl(query, app=app, flow=flow)

    reg.register(
        HandSpec(
            name="search_knowledge",
            description="Search local knowledge markdown and Chroma RAG.",
            risk=RiskTier.READ,
            capability="knowledge",
            tool=make_tool(
                name="search_knowledge",
                description="Search local PE knowledge / RAG.",
                func=lambda query, app="", flow="": _search_knowledge(query, app, flow),
                args_schema=SearchKnowledgeArgs,
            ),
        )
    )

    def _trends(
        question: str = "trend report",
        app: str = "",
        flow: str = "",
        exclude_smoke: bool = False,
        min_vus: int = 0,
    ) -> str:
        from src.utils.perf_evidence import gather_evidence_for_question

        app = app or str(state.get("app") or "")
        flow = flow or str(state.get("flow") or "default")
        q = question
        if exclude_smoke:
            q += " exclude smoke"
        if min_vus:
            q += f" {min_vus} users"
        pack = gather_evidence_for_question(q, app=app, flow=flow)
        return _ok(
            {
                "markdown": pack.get("trend_markdown"),
                "notes": pack.get("notes"),
                "kpis": pack.get("kpis"),
                "source": "hand:get_run_trends",
            }
        )

    reg.register(
        HandSpec(
            name="get_run_trends",
            description="Get KPI / trend table from local knowledge (and sync cues).",
            risk=RiskTier.READ,
            capability="trends",
            tool=make_tool(
                name="get_run_trends",
                description="KPI trend table from local run history.",
                func=_trends,
                args_schema=TrendsArgs,
            ),
        )
    )

    def _sync_confluence(
        question: str = "",
        app: str = "",
        flow: str = "",
        exclude_smoke: bool = False,
        min_vus: int = 0,
    ) -> str:
        from src.utils.perf_evidence import sync_confluence_and_build_report

        app = app or str(state.get("app") or "")
        flow = flow or str(state.get("flow") or "default")
        report = sync_confluence_and_build_report(
            app=app,
            flow=flow,
            question=question or "trend from confluence",
            force=True,
            exclude_smoke=exclude_smoke,
            min_vus=min_vus,
        )
        return _ok({**report, "source": "hand:sync_confluence_trends"})

    reg.register(
        HandSpec(
            name="sync_confluence_trends",
            description="Sync Confluence Run pages into knowledge/RAG and return a trend table.",
            risk=RiskTier.MUTATE,
            capability="confluence",
            requires_confirm_unless_authorized=True,
            auth_keys=["publish_confluence"],
            tool=make_tool(
                name="sync_confluence_trends",
                description="Sync Confluence runs and build KPI trend markdown.",
                func=_sync_confluence,
                args_schema=TrendsArgs,
            ),
        )
    )

    # --- Jira read ---
    def _list_stories() -> str:
        from src.tools.pe_assistant_tools import _list_jira_stories_impl

        return _list_jira_stories_impl(assist_fallback=True)

    reg.register(
        HandSpec(
            name="list_jira_stories",
            description="List eligible / board Jira stories (REST).",
            risk=RiskTier.READ,
            capability="jira",
            tool=make_tool(
                name="list_jira_stories",
                description="List Jira stories for PE work.",
                func=lambda: _list_stories(),
                args_schema=EmptyArgs,
            ),
        )
    )

    def _search_jira(query: str = "", jql: str = "", max_results: int = 15) -> str:
        from src.tools.pe_assistant_tools import _search_jira_impl

        return _search_jira_impl(query=query, jql=jql, max_results=max_results)

    # Reuse QueryArgs-like — extend via kwargs on existing search
    class _JiraSearch(QueryArgs):
        jql: str = ""
        max_results: int = 15

    reg.register(
        HandSpec(
            name="search_jira",
            description="Search Jira by text or JQL (not just To Do list).",
            risk=RiskTier.READ,
            capability="jira",
            tool=make_tool(
                name="search_jira",
                description="Search Jira issues by free text or JQL.",
                func=lambda query="", jql="", max_results=15: _search_jira(
                    query=query, jql=jql, max_results=max_results
                ),
                args_schema=_JiraSearch,
            ),
        )
    )

    def _get_issue(issue_key: str) -> str:
        from src.tools.pe_assistant_tools import _get_jira_issue_impl

        return _get_jira_issue_impl(issue_key)

    reg.register(
        HandSpec(
            name="get_jira_issue",
            description="Get one Jira issue summary/status/labels.",
            risk=RiskTier.READ,
            capability="jira",
            tool=make_tool(
                name="get_jira_issue",
                description="Fetch a Jira issue by key.",
                func=_get_issue,
                args_schema=IssueKeyArgs,
            ),
        )
    )

    def _get_comments(issue_key: str) -> str:
        from src.tools.pe_assistant_tools import _get_jira_comments_impl

        return _get_jira_comments_impl(issue_key)

    reg.register(
        HandSpec(
            name="get_jira_comments",
            description="Get recent comments on a Jira issue (findings live here).",
            risk=RiskTier.READ,
            capability="jira",
            tool=make_tool(
                name="get_jira_comments",
                description="List recent Jira comments.",
                func=_get_comments,
                args_schema=IssueKeyArgs,
            ),
        )
    )

    def _rank_stories(user_goal: str) -> str:
        from src.tools.pe_assistant_tools import _list_jira_stories_impl

        raw = _list_jira_stories_impl(assist_fallback=True)
        try:
            data = json.loads(raw)
        except Exception:
            data = {"markdown": raw, "keys": []}
        keys = list(data.get("keys") or [])
        md = str(data.get("markdown") or "")
        goal = (user_goal or "").lower()
        scored: List[Dict[str, Any]] = []
        for key in keys:
            # naive score from markdown line containing key
            line = ""
            for ln in md.splitlines():
                if key in ln:
                    line = ln
                    break
            blob = line.lower()
            score = 0.0
            for tok in re.findall(r"[a-z0-9-]{3,}", goal):
                if tok in blob:
                    score += 1.0
            if "performance" in goal and (
                "performance" in blob or "nfe" in blob or "load" in blob
            ):
                score += 0.5
            if "claim" in goal and "claim" in blob:
                score += 1.5
            scored.append({"key": key, "score": score, "line": line})
        scored.sort(key=lambda x: -float(x["score"]))
        best = scored[0] if scored else None
        ambiguous = len(scored) > 1 and (
            not best
            or (
                len(scored) > 1
                and abs(float(scored[0]["score"]) - float(scored[1]["score"])) < 0.75
            )
            or float((best or {}).get("score") or 0) < 1.0
        )
        return _ok(
            {
                "candidates": scored[:8],
                "recommended_key": None if ambiguous else (best or {}).get("key"),
                "ambiguous": ambiguous or not scored,
                "markdown": md,
                "source": "hand:rank_jira_stories",
            }
        )

    reg.register(
        HandSpec(
            name="rank_jira_stories",
            description="Rank board/eligible stories against the user goal; flags ambiguity.",
            risk=RiskTier.READ,
            capability="jira",
            tool=make_tool(
                name="rank_jira_stories",
                description="Rank Jira stories for the user goal.",
                func=_rank_stories,
                args_schema=RankStoriesArgs,
            ),
        )
    )

    async def _execute_story_async(issue_key: str, force: bool = False) -> str:
        from src.integrations.jira.worker import process_issue_key

        key = (issue_key or "").strip().upper()
        if not key:
            return _ok({"error": "issue_key required"})
        try:
            result = await process_issue_key(key, force=force)
        except Exception as exc:
            logger.exception("execute_jira_story failed")
            return _ok({"error": redact_text_for_llm(str(exc)), "issue_key": key})
        if isinstance(result, dict):
            summary = {
                "issue_key": key,
                "ok": result.get("ok", result.get("success")),
                "skipped": result.get("skipped"),
                "reason": result.get("reason"),
                "error": result.get("error"),
                "message": redact_text_for_llm(
                    str(result.get("message") or result.get("summary") or result.get("error") or "")
                )[:2000],
                "confluence_url": result.get("confluence_url")
                or (result.get("confluence") or {}).get("run_url"),
                "raw_keys": list(result.keys())[:40],
                "source": "hand:execute_jira_story",
            }
            return _ok(summary)
        return _ok(
            {
                "issue_key": key,
                "ok": True,
                "message": redact_text_for_llm(str(result))[:2000],
                "source": "hand:execute_jira_story",
            }
        )

    exec_tool = make_tool(
        name="execute_jira_story",
        description="Run the NFE Jira story pipeline for a key.",
        func=lambda issue_key, force=False: "(use async)",
        args_schema=ExecuteStoryArgs,
    )
    exec_tool.coroutine = _execute_story_async  # type: ignore[method-assign]
    exec_tool.func = lambda issue_key, force=False: asyncio_run_safe(  # type: ignore[method-assign]
        _execute_story_async(issue_key, force)
    )

    reg.register(
        HandSpec(
            name="execute_jira_story",
            description="Execute a Jira PE story (record/analyse/k6/smoke) via the product worker.",
            risk=RiskTier.EXECUTE,
            capability="jira",
            requires_confirm_unless_authorized=True,
            auth_keys=["execute_story"],
            tool=exec_tool,
        )
    )

    # --- Create analysis issue ---
    def _create_issue(
        summary: str,
        description: str,
        acceptance_criteria: str = "",
        parent_key: str = "",
        labels: str = "nfe-analysis",
    ) -> str:
        from config.settings import settings

        if not getattr(settings, "NFE_JIRA_CREATE_ENABLED", False):
            return _ok(
                {
                    "error": "NFE_JIRA_CREATE_ENABLED is false — create blocked",
                    "draft": {
                        "summary": summary,
                        "description": description[:3000],
                        "acceptance_criteria": acceptance_criteria[:2000],
                        "parent_key": parent_key,
                    },
                }
            )
        from src.integrations.jira.client import JiraClient

        labs = [x.strip() for x in (labels or "").split(",") if x.strip()]
        try:
            client = JiraClient()
            created = client.create_issue(
                summary=summary,
                description=description,
                acceptance_criteria=acceptance_criteria,
                labels=labs,
                parent_key=parent_key or None,
            )
            if parent_key and created.get("key"):
                try:
                    client.add_comment(
                        parent_key,
                        f"NFE opened analysis issue **{created['key']}**: {summary}",
                    )
                except Exception as exc:
                    logger.warning("Parent comment failed: %s", exc)
            return _ok({**created, "source": "hand:create_jira_issue"})
        except Exception as exc:
            return _ok({"error": redact_text_for_llm(str(exc))})

    reg.register(
        HandSpec(
            name="create_jira_issue",
            description="Create a Jira analysis issue (requires NFE_JIRA_CREATE_ENABLED + authorization).",
            risk=RiskTier.MUTATE,
            capability="jira",
            requires_confirm_unless_authorized=True,
            auth_keys=["create_analysis_issue"],
            tool=make_tool(
                name="create_jira_issue",
                description="Create a Jira issue for PE analysis / RCA.",
                func=_create_issue,
                args_schema=CreateIssueArgs,
            ),
        )
    )

    def _comment_jira(issue_key: str, body: str) -> str:
        from src.integrations.jira.client import JiraClient

        key = (issue_key or "").strip().upper()
        if not key or not (body or "").strip():
            return _ok({"error": "issue_key and body required"})
        try:
            JiraClient().add_comment(key, body)
            return _ok(
                {
                    "ok": True,
                    "issue_key": key,
                    "source": "hand:comment_jira_issue",
                }
            )
        except Exception as exc:
            return _ok({"error": redact_text_for_llm(str(exc)), "issue_key": key})

    reg.register(
        HandSpec(
            name="comment_jira_issue",
            description="Post a comment on a Jira issue (findings / RCA link).",
            risk=RiskTier.MUTATE,
            capability="jira",
            requires_confirm_unless_authorized=True,
            auth_keys=["execute_story", "create_analysis_issue"],
            tool=make_tool(
                name="comment_jira_issue",
                description="Comment on a Jira issue.",
                func=_comment_jira,
                args_schema=JiraCommentArgs,
            ),
        )
    )

    async def _run_smoke_async(script_path: str = "") -> str:
        from src.utils.k6_mcp import run_k6_smoke_preferred

        path = (script_path or "").strip()
        if not path:
            perf = state.get("performance_test_output") or {}
            arts = (perf.get("artifacts") or {}) if isinstance(perf, dict) else {}
            k6_file = arts.get("k6_file") if isinstance(arts, dict) else None
            if isinstance(k6_file, dict):
                path = str(k6_file.get("path") or "")
            elif isinstance(k6_file, str):
                path = k6_file
        if not path:
            return _ok(
                {
                    "error": "No k6 script path; pass script_path or run analyse first",
                    "source": "hand:run_local_k6_smoke",
                }
            )
        try:
            result = await run_k6_smoke_preferred(path)
            return _ok(
                {
                    "ok": (result or {}).get("ok"),
                    "summary": redact_text_for_llm(str((result or {}).get("summary") or ""))[
                        :2000
                    ],
                    "failed_urls": (result or {}).get("failed_urls"),
                    "script_path": path,
                    "source": "hand:run_local_k6_smoke",
                }
            )
        except Exception as exc:
            return _ok({"error": redact_text_for_llm(str(exc)), "script_path": path})

    smoke_tool = make_tool(
        name="run_local_k6_smoke",
        description="Run local k6 smoke against a script path or session artifact.",
        func=lambda script_path="": "(use async)",
        args_schema=SmokeArgs,
    )
    smoke_tool.coroutine = _run_smoke_async  # type: ignore[method-assign]
    smoke_tool.func = lambda script_path="": asyncio_run_safe(  # type: ignore[method-assign]
        _run_smoke_async(script_path)
    )
    reg.register(
        HandSpec(
            name="run_local_k6_smoke",
            description="Execute local k6 smoke (assertion gate) for a script.",
            risk=RiskTier.EXECUTE,
            capability="scripting",
            requires_confirm_unless_authorized=True,
            auth_keys=["execute_story"],
            tool=smoke_tool,
        )
    )

    # --- Structured report ---
    def _format_report(query: str = "") -> str:
        smoke = {}
        perf = state.get("performance_test_output") or {}
        if isinstance(perf, dict):
            smoke = perf.get("smoke_result") or perf.get("k6_smoke") or {}
        # Also accept last execute result embedded in state
        report = {
            "title": "PE run report",
            "query_hint": query,
            "target_url": state.get("target_url"),
            "app": state.get("app"),
            "flow": state.get("flow"),
            "jira_issue_key": state.get("jira_issue_key"),
            "smoke_ok": smoke.get("ok") if isinstance(smoke, dict) else None,
            "summary": redact_text_for_llm(str((smoke or {}).get("summary") or ""))[:1500]
            if isinstance(smoke, dict)
            else "",
            "failed_urls": (smoke or {}).get("failed_urls") if isinstance(smoke, dict) else [],
            "heal_notes": (smoke or {}).get("heal_notes") if isinstance(smoke, dict) else [],
            "source": "hand:format_run_report",
        }
        lines = [
            "### PE run report",
            f"- **App/flow:** `{report['app']}` / `{report['flow']}`",
            f"- **Story:** `{report['jira_issue_key'] or 'n/a'}`",
            f"- **Smoke ok:** `{report['smoke_ok']}`",
            f"- **Summary:** {report['summary'] or 'n/a'}",
        ]
        if report["failed_urls"]:
            lines.append("- **Failed URLs:**")
            for u in list(report["failed_urls"])[:8]:
                lines.append(f"  - `{u}`")
        if report["heal_notes"]:
            lines.append("- **Heal notes:**")
            for n in list(report["heal_notes"])[:8]:
                lines.append(f"  - {n}")
        return _ok({"markdown": "\n".join(lines), **report})

    reg.register(
        HandSpec(
            name="format_run_report",
            description="Format a structured pass/fail PE report from session smoke/artifacts.",
            risk=RiskTier.READ,
            capability="report",
            tool=make_tool(
                name="format_run_report",
                description="Build structured PE run report markdown.",
                func=lambda query="": _format_report(query),
                args_schema=QueryArgs,
            ),
        )
    )

    # --- Recordings / Watch-me (advisory + queue hint; full headed browser stays pipeline) ---
    def _list_recordings(app: str = "", flow: str = "") -> str:
        from src.utils.app_registry import artifacts_root, list_knowledge_apps

        root = artifacts_root() / "recordings"
        apps = [app] if app else list_knowledge_apps()
        if not apps and root.is_dir():
            apps = [p.name for p in root.iterdir() if p.is_dir()]
        items = []
        for a in apps:
            d = root / a
            if not d.is_dir():
                continue
            for f in sorted(d.glob("*.json"))[:20]:
                if flow and flow not in f.stem:
                    continue
                items.append({"app": a, "name": f.name, "stem": f.stem})
        return _ok({"items": items, "source": "hand:list_recordings"})

    reg.register(
        HandSpec(
            name="list_recordings",
            description="List saved Watch-me / journey recordings on disk.",
            risk=RiskTier.READ,
            capability="playwright",
            tool=make_tool(
                name="list_recordings",
                description="List recording JSON files.",
                func=lambda app="", flow="": _list_recordings(app, flow),
                args_schema=AppFlowArgs,
            ),
        )
    )

    def _request_watch_me(target_url: str, label: str = "") -> str:
        # Queue intent for the graph — agent cannot open headed browser in-process safely here
        return _ok(
            {
                "queued": True,
                "action": "watch_me",
                "target_url": target_url,
                "label": label,
                "message": (
                    "Watch-me must run as a headed browser session. "
                    "Ask the user to confirm, then they (or Studio) should run "
                    f"`watch me {label + ' ' if label else ''}{target_url}`. "
                    "Alternatively set pending_action for channel handoff."
                ),
                "handoff_phrase": f"watch me {label} {target_url}".strip(),
                "source": "hand:request_watch_me",
            }
        )

    reg.register(
        HandSpec(
            name="request_watch_me",
            description="Prepare a Watch-me recording handoff (headed browser) for the given URL.",
            risk=RiskTier.EXECUTE,
            capability="playwright",
            requires_confirm_unless_authorized=True,
            auth_keys=["watch_me"],
            tool=make_tool(
                name="request_watch_me",
                description="Queue Watch-me recording for a URL.",
                func=_request_watch_me,
                args_schema=WatchMeArgs,
            ),
        )
    )

    def _reuse_recording(app: str = "", flow: str = "default") -> str:
        return _ok(
            {
                "queued": True,
                "action": "reuse_recording",
                "app": app or state.get("app"),
                "flow": flow,
                "message": (
                    "To reuse a recording in Studio, say: "
                    f"`analyse saved recording {app or state.get('app') or '<app>'} {flow}`."
                ),
                "handoff_phrase": (
                    f"analyse saved recording {app or state.get('app') or ''} {flow}"
                ).strip(),
                "source": "hand:reuse_recording",
            }
        )

    reg.register(
        HandSpec(
            name="reuse_recording",
            description="Prepare reuse of a saved recording for analyse/k6 generation.",
            risk=RiskTier.EXECUTE,
            capability="playwright",
            requires_confirm_unless_authorized=True,
            auth_keys=["execute_story", "watch_me"],
            tool=make_tool(
                name="reuse_recording",
                description="Handoff to reuse a saved recording.",
                func=_reuse_recording,
                args_schema=ReuseRecordingArgs,
            ),
        )
    )

    return reg
