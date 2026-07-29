"""
Answers follow-up questions about a completed performance analysis
using existing state (no browser re-run).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

from src.utils.model_router import get_model_router, TaskType
from src.utils.prompt_loader import render_prompt

logger = logging.getLogger(__name__)


def _summarize_analysis_context(state: Dict[str, Any]) -> str:
    """Build a compact context pack from prior analysis state.

    Args:
        state: Current pipeline state containing prior captures and analysis.

    Returns:
        A bounded JSON string suitable for the QA model prompt.
    """
    from src.security.secrets import redact_text_for_llm

    perf = state.get("performance_test_output") or {}
    arts = perf.get("artifacts") or {}
    smoke = perf.get("k6_smoke") or state.get("k6_smoke") or {}
    confluence = perf.get("confluence") or {}
    ir = perf.get("load_test_ir") or state.get("load_test_ir") or {}
    if not isinstance(ir, dict):
        ir = {}
    workload = ir.get("workload") or {}

    k6_file = arts.get("k6_file") if isinstance(arts.get("k6_file"), dict) else {}
    payload: Dict[str, Any] = {
        "target_url": state.get("target_url"),
        "app": state.get("app"),
        "flow": state.get("flow") or state.get("recording_label"),
        "sub_tasks": state.get("sub_tasks") or [],
        "parameterization": [],
        "correlations": {
            "traced_dependencies": [],
            "uncorrelated_or_dynamic": [],
            "summary": "",
        },
        "transactions": state.get("transactions")
        or perf.get("transactions")
        or [],
        "artifacts": {
            "has_har": bool(arts.get("har")),
            "has_k6_script": bool(arts.get("k6_script")),
            "k6_path": k6_file.get("path") or arts.get("k6_path") or "",
            "ir_path": arts.get("ir_path") or "",
            "html_report": smoke.get("html_report") or "",
            "summary_json": smoke.get("summary_json") or "",
        },
        "k6_smoke": {
            "ok": smoke.get("ok"),
            "skipped": smoke.get("skipped"),
            "summary": str(smoke.get("summary") or "")[:800],
            "failed_checks": list(smoke.get("failed_checks") or [])[:20],
            "failed_urls": list(smoke.get("failed_urls") or [])[:20],
            "status_counts": dict(smoke.get("status_counts") or {}),
            "heal_notes": list(smoke.get("heal_notes") or [])[:15],
            "exit_code": smoke.get("exit_code"),
            "assertion_gate_failed": smoke.get("assertion_gate_failed"),
        },
        "workload": {
            "vus": workload.get("vus"),
            "iterations": workload.get("iterations"),
            "executor": workload.get("executor"),
            "pacing_s": workload.get("pacing_s"),
            "think_time_s": workload.get("think_time_s"),
            "thresholds": workload.get("thresholds") or {},
        },
        "confluence": {
            "published": confluence.get("published"),
            "run_url": confluence.get("run_url") or "",
            "flow_url": confluence.get("flow_url") or "",
            "skipped_reason": confluence.get("skipped_reason") or "",
        },
        "evidence_sources": ["session"],
    }

    for cand in state.get("parameterizable_candidates") or []:
        payload["parameterization"].append(
            {
                "variable": cand.get("variable_name"),
                "selector": cand.get("selector"),
                "value": cand.get("value"),
                "is_credential": cand.get("is_credential"),
                "propagations": cand.get("propagations") or [],
            }
        )

    deps = state.get("dependencies") or []
    for dep in deps[:40]:
        payload["correlations"]["traced_dependencies"].append(
            {
                "variable": dep.get("value_key"),
                "extract_from": {
                    "request": dep.get("source_request"),
                    "location": dep.get("source_location"),
                },
                "pass_to": {
                    "request": dep.get("target_request"),
                    "location": dep.get("target_location"),
                },
                "run1_value": dep.get("run1_value"),
                "run2_value": dep.get("run2_value"),
                "type": dep.get("correlation_type"),
            }
        )

    # Prefer the normalized report structure because it may contain richer
    # post-processing than the raw detector state.
    corr = perf.get("correlation") or {}
    if corr.get("extract_pass"):
        payload["correlations"]["extract_pass"] = corr.get("extract_pass")
    if corr.get("uncorrelated_dynamics"):
        payload["correlations"]["uncorrelated_or_dynamic"] = corr.get(
            "uncorrelated_dynamics"
        )[:30]
    elif state.get("correlations"):
        for c in (state.get("correlations") or [])[:30]:
            payload["correlations"]["uncorrelated_or_dynamic"].append(
                {
                    "variable": c.get("dynamic_name"),
                    "request": c.get("request_url"),
                    "location": f"{c.get('location')}.{c.get('key')}",
                    "run1": c.get("run1_value"),
                    "run2": c.get("run2_value"),
                }
            )

    if not deps and not payload["correlations"]["uncorrelated_or_dynamic"]:
        payload["correlations"]["summary"] = (
            "No traced correlations were found between Run 1 and Run 2. "
            "That often means login used cookies/session storage without a reusable "
            "token in responses, or traffic was mostly static HTML / client-side."
        )
    elif not deps:
        payload["correlations"]["summary"] = (
            "Dynamic values were detected but no extract→pass (response→request) "
            "correlation chain was traced."
        )
    else:
        payload["correlations"]["summary"] = (
            f"{len(deps)} extract→pass correlation link(s) were traced."
        )

    # Keep prompt size bounded + redact secrets
    text = json.dumps(payload, indent=2, default=str)
    text = redact_text_for_llm(text)
    if len(text) > 12000:
        text = text[:12000] + "\n... [truncated]"
    return text


def _resolve_app_flow(state: Dict[str, Any]) -> tuple[str, str]:
    app = str(state.get("app") or "")
    flow = str(state.get("flow") or state.get("recording_label") or "")
    target_url = str(state.get("target_url") or "")
    if not app and target_url:
        try:
            from src.utils.app_registry import resolve_app_and_flow

            app, flow = resolve_app_and_flow(
                target_url=target_url,
                label=flow,
            )
        except Exception:
            pass
    return app, flow or "default"


def _knowledge_context(state: Dict[str, Any], question: str) -> str:
    """Build markdown + RAG + local trend context for Analysis QA (soft-fails)."""
    parts: List[str] = []
    sources: List[str] = []
    app, flow = _resolve_app_flow(state)
    target_url = str(state.get("target_url") or "")

    try:
        from src.utils.knowledge_store import read_flow, read_overview

        if app and flow:
            card = read_flow(app, flow)
            if card:
                sources.append("knowledge_markdown")
                parts.append(f"### Direct flow card (`{app}/{flow}`)\n\n{card}")
        if app:
            overview = read_overview(app)
            if overview:
                if len(overview) > 1500:
                    overview = overview[:1500] + "\n... [truncated]"
                parts.append(f"### App overview (`{app}`)\n\n{overview}")
    except Exception as exc:
        logger.debug("Knowledge markdown load skipped: %s", exc)

    # Local trends + optional Confluence refresh (cache-first)
    try:
        from src.utils.perf_evidence import gather_evidence_for_question
        from src.utils.perf_trend import wants_trend_question

        if app and (
            wants_trend_question(question)
            or "result" in (question or "").lower()
            or "p95" in (question or "").lower()
            or "smoke" in (question or "").lower()
        ):
            evidence = gather_evidence_for_question(
                question,
                app=app,
                flow=flow,
                target_url=target_url,
            )
            for s in evidence.get("sources") or []:
                if s not in sources:
                    sources.append(s)
            trend_md = evidence.get("trend_markdown") or ""
            notes = evidence.get("notes") or []
            block = ["### Local run history / trends", ""]
            if sources:
                block.append(f"_Evidence sources: {', '.join(sources)}_")
                block.append("")
            if trend_md:
                block.append(trend_md)
            if notes:
                block.append("")
                block.append("**Notes:**")
                for n in notes:
                    block.append(f"- {n}")
            parts.append("\n".join(block))
    except Exception as exc:
        logger.debug("Trend/evidence pack skipped: %s", exc)

    try:
        from src.utils.rag_store import query as rag_query

        hits = rag_query(question or "", app=app or None)
        if hits:
            if "rag" not in sources:
                sources.append("rag")
            lines = ["### Retrieved from knowledge (RAG)", ""]
            for i, hit in enumerate(hits, 1):
                meta = hit.get("metadata") or {}
                src = meta.get("path") or f"{meta.get('app')}/{meta.get('flow')}"
                lines.append(f"**[{i}] Retrieved from `{src}`**")
                lines.append(str(hit.get("text") or "")[:1200])
                lines.append("")
            parts.append("\n".join(lines))
    except Exception as exc:
        logger.debug("RAG query skipped: %s", exc)

    if not parts:
        return (
            "(no knowledge cards, run history, or RAG hits for this app yet — "
            "complete a smoke run to ingest KPIs locally)"
        )
    text = "\n\n".join(parts)
    if len(text) > 10000:
        text = text[:10000] + "\n... [truncated]"
    return text


class AnalysisQAAgent:
    """Answers follow-up questions from existing pipeline state without reruns."""

    async def _rebuild_txn_and_k6(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild transactions, load-test IR, and k6 output from captures.

        Args:
            state: Pipeline state containing journey steps and network records.

        Returns:
            Fresh transactions, IR, k6 script, and optional artifact metadata.
        """
        from src.agents.transaction_agent import TransactionAgent
        from src.utils.k6_generator import generate_k6_script
        from src.utils.load_test_ir import build_load_test_ir
        from src.utils.artifacts import save_k6_script, save_load_test_ir
        from src.utils.app_registry import resolve_app_and_flow

        records = state.get("run_records") or []
        network = []
        if records:
            network = records[0].get("network_requests") or []
        user_steps = state.get("user_journey_steps") or []
        sub_tasks = state.get("sub_tasks") or []
        target_url = state.get("target_url") or ""
        app, flow = resolve_app_and_flow(
            target_url=target_url,
            label=state.get("flow") or state.get("recording_label") or "",
            explicit_app=state.get("app") or "",
        )

        txn_agent = TransactionAgent()
        transactions = await txn_agent.group_transactions(
            target_url=target_url,
            user_steps=user_steps,
            sub_tasks=sub_tasks,
            network_requests=network,
        )
        load_test_ir = build_load_test_ir(
            target_url=target_url,
            parameterizable_candidates=state.get("parameterizable_candidates") or [],
            dependencies=state.get("dependencies") or [],
            transactions=transactions,
            network_requests=network,
            credentials=state.get("credentials") or {},
        )
        k6_script = generate_k6_script(
            target_url=target_url,
            parameterizable_candidates=state.get("parameterizable_candidates") or [],
            dependencies=state.get("dependencies") or [],
            transactions=transactions,
            network_requests=network,
            ir=load_test_ir,
        )
        k6_file: Dict[str, str] = {}
        try:
            k6_file = save_k6_script(
                k6_script, target_url=target_url, app=app, flow=flow
            )
            save_load_test_ir(
                load_test_ir, target_url=target_url, app=app, flow=flow
            )
        except Exception as art_err:
            logger.warning("Failed to write k6 artifact: %s", art_err)

        return {
            "transactions": transactions,
            "k6_script": k6_script,
            "load_test_ir": load_test_ir,
            "k6_file": k6_file,
        }

    async def answer(self, question: str, state: Dict[str, Any]) -> str:
        """Answer a question about a completed NFE analysis.

        Args:
            question: User question, including optional transaction or k6 intent.
            state: Existing pipeline state and generated artifacts.

        Returns:
            A Markdown answer generated from state, rebuilt artifacts, or a
            deterministic fallback when the model is unavailable.
        """
        if not state.get("target_url") and not state.get("performance_test_output"):
            return (
                "I don’t have a prior analysis in this chat yet. "
                "Paste a target URL and journey steps first, then ask follow-up questions."
            )

        q = (question or "").lower()
        wants_txn = bool(
            re.search(r"\b(txn|txns|transaction|transactions|grouping|group\s+request)\b", q)
        )
        wants_k6 = bool(
            re.search(r"\b(k6|load\s*script|jmeter|gatling|script\s+stub|generate\s+script)\b", q)
        )

        # Transaction and script requests require regeneration from the capture;
        # serving a stored placeholder can misrepresent the actual request flow.
        if wants_txn or wants_k6:
            from src.utils.formatting import format_transactions_section, format_k6_section

            parts: List[str] = []
            rebuilt: Dict[str, Any] = {}
            records = state.get("run_records") or []
            if records and (records[0].get("network_requests") or state.get("user_journey_steps")):
                try:
                    rebuilt = await self._rebuild_txn_and_k6(state)
                except Exception as exc:
                    logger.warning("TXN/k6 rebuild failed (%s); using stored artifacts.", exc)

            if wants_txn:
                txns = (
                    rebuilt.get("transactions")
                    or state.get("transactions")
                    or (state.get("performance_test_output") or {}).get("transactions")
                    or []
                )
                if not txns:
                    parts.append(
                        "_No transactions available. Re-run the journey analysis first "
                        "so network traffic can be captured per phase._"
                    )
                else:
                    note = ""
                    if any(t.get("http_entries") for t in txns):
                        note = (
                            "_Business phases — user actions only "
                            "(HTTP detail is in k6/IR, not listed here)._\n\n"
                        )
                    parts.append(note + format_transactions_section(txns))

            if wants_k6:
                k6 = rebuilt.get("k6_script") or (
                    ((state.get("performance_test_output") or {}).get("artifacts") or {}).get(
                        "k6_script"
                    )
                    or ""
                )
                k6_file = rebuilt.get("k6_file") or (
                    ((state.get("performance_test_output") or {}).get("artifacts") or {}).get(
                        "k6_file"
                    )
                    or {}
                )
                parts.append(
                    format_k6_section(
                        k6,
                        file_path=k6_file.get("path", ""),
                        file_url=k6_file.get("file_url", ""),
                        relative_path=k6_file.get("relative_path", ""),
                    )
                )

            return "\n\n".join(parts)

        context = _summarize_analysis_context(state)
        knowledge = _knowledge_context(state, question)
        prompt = render_prompt(
            "analysis_qa",
            context=context,
            knowledge=knowledge,
            question=question,
        )
        router = get_model_router()
        try:
            response = await router.ainvoke_with_failover(
                TaskType.EXTRACTION,
                lambda model: model,
                prompt,
            )
            content = getattr(response, "content", response)
            if isinstance(content, list):
                # Multimodal/chat providers may return content blocks rather
                # than one text string; retain only textual blocks for the UI.
                parts = []
                for block in content:
                    if isinstance(block, str):
                        parts.append(block)
                    elif isinstance(block, dict) and "text" in block:
                        parts.append(str(block["text"]))
                return "\n".join(parts).strip() or str(content)
            return str(content).strip()
        except Exception as exc:
            logger.error("Analysis QA failed: %s", exc)
            return self._fallback_answer(question, state)

    def _fallback_answer(self, question: str, state: Dict[str, Any]) -> str:
        """Build a deterministic answer when model-based QA fails.

        Args:
            question: User question used to select relevant result categories.
            state: Existing pipeline analysis state.

        Returns:
            A Markdown summary of matching correlations or parameters.
        """
        deps = state.get("dependencies") or []
        params = state.get("parameterizable_candidates") or []
        q = question.lower()

        lines = [
            f"Based on the last analysis for `{state.get('target_url', 'the target site')}`:\n"
        ]

        if any(k in q for k in ("token", "auth", "login", "session", "cookie", "csrf", "correlat", "corelat")):
            if not deps:
                lines.append(
                    "**No authentication token correlation was detected** between Run 1 and Run 2.\n\n"
                    "That usually means:\n"
                    "- Login may rely on **cookies / session storage** set by the browser rather than a "
                    "token extracted from a JSON response body\n"
                    "- Or the app uses static form posts without a CSRF/bearer token in subsequent requests\n"
                    "- Or the token field name wasn’t present in captured traffic\n\n"
                    "If auth is cookie-based, you typically **correlate/manage the cookie jar** in the load tool "
                    "instead of extracting a bearer token. Parameterize username/password; let the tool handle cookies.\n"
                )
            else:
                auth_deps = [
                    d for d in deps
                    if any(
                        t in str(d.get("value_key", "")).lower()
                        or t in str(d.get("source_location", "")).lower()
                        or t in str(d.get("target_location", "")).lower()
                        for t in ("token", "auth", "csrf", "session", "cookie", "jwt")
                    )
                ]
                if auth_deps:
                    lines.append("**Auth-related correlations found:**\n")
                    for d in auth_deps[:8]:
                        lines.append(
                            f"- `{d.get('value_key')}`: extract `{d.get('source_location')}` "
                            f"from `{d.get('source_request')}` → pass to `{d.get('target_location')}` "
                            f"in `{d.get('target_request')}`"
                        )
                else:
                    lines.append(
                        f"There are **{len(deps)} correlation(s)** overall, but none clearly look like "
                        "an auth bearer/CSRF token. Check cookies/session handling for login.\n"
                    )

        if params and any(k in q for k in ("param", "credential", "user", "password", "input")):
            lines.append("\n**Parameterization from the last run:**\n")
            seen = set()
            for p in params:
                key = (p.get("selector"), p.get("value"))
                if key in seen:
                    continue
                seen.add(key)
                lines.append(
                    f"- `{p.get('variable_name')}` ← `{p.get('selector')}` = `{p.get('value')}`"
                )

        smoke = (state.get("performance_test_output") or {}).get("k6_smoke") or {}
        if smoke and any(
            k in q for k in ("smoke", "p95", "result", "fail", "sla", "report", "trend")
        ):
            lines.append("\n**Last smoke / results (session):**\n")
            lines.append(f"- ok: `{smoke.get('ok')}` skipped: `{smoke.get('skipped')}`")
            if smoke.get("summary"):
                lines.append(f"- summary: {smoke.get('summary')}")
            fails = smoke.get("failed_checks") or []
            if fails:
                lines.append("- failed checks: " + ", ".join(f"`{f}`" for f in fails[:8]))

        if len(lines) == 1:
            lines.append(
                "I still have the prior analysis in context. Ask about correlations, tokens, "
                "parameters, smoke results, trends, or a specific request — or say **run again** "
                "to re-execute the journey."
            )
        return "\n".join(lines)
