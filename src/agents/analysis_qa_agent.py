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
    payload: Dict[str, Any] = {
        "target_url": state.get("target_url"),
        "sub_tasks": state.get("sub_tasks") or [],
        "parameterization": [],
        "correlations": {
            "traced_dependencies": [],
            "uncorrelated_or_dynamic": [],
            "summary": "",
        },
        "transactions": state.get("transactions")
        or (state.get("performance_test_output") or {}).get("transactions")
        or [],
        "artifacts": {
            "has_har": bool(
                ((state.get("performance_test_output") or {}).get("artifacts") or {}).get("har")
            ),
            "has_k6_script": bool(
                ((state.get("performance_test_output") or {}).get("artifacts") or {}).get(
                    "k6_script"
                )
            ),
        },
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
    perf = state.get("performance_test_output") or {}
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

    # Keep prompt size bounded
    text = json.dumps(payload, indent=2, default=str)
    if len(text) > 12000:
        text = text[:12000] + "\n... [truncated]"
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

        records = state.get("run_records") or []
        network = []
        if records:
            network = records[0].get("network_requests") or []
        user_steps = state.get("user_journey_steps") or []
        sub_tasks = state.get("sub_tasks") or []
        target_url = state.get("target_url") or ""

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
            k6_file = save_k6_script(k6_script, target_url=target_url)
            save_load_test_ir(load_test_ir, target_url=target_url)
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
                            "_Built from captured network traffic (METHOD+URL per phase). "
                            "UI-only SPA phases may also appear._\n\n"
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
        prompt = render_prompt(
            "analysis_qa",
            context=context,
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

        if len(lines) == 1:
            lines.append(
                "I still have the prior analysis in context. Ask about correlations, tokens, "
                "parameters, or a specific request — or say **run again** to re-execute the journey."
            )
        return "\n".join(lines)
