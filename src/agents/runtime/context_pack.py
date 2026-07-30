"""Shared context pack built once per PE assistant turn."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def build_context_pack(
    state: Dict[str, Any],
    question: str,
    *,
    include_knowledge: bool = True,
) -> Dict[str, Any]:
    """Build a one-shot context pack for supervisor + specialists.

    Args:
        state: LangGraph / AgentState dictionary.
        question: Latest user question.
        include_knowledge: Whether to attach knowledge/RAG slices.

    Returns:
        Structured pack with session, knowledge, and meta slices.
    """
    from src.agents.analysis_qa_agent import (
        _knowledge_context,
        _resolve_app_flow,
        _summarize_analysis_context,
    )

    app, flow = _resolve_app_flow(state)
    target_url = str(state.get("target_url") or "")
    try:
        from src.utils.app_registry import resolve_evidence_scope

        app, flow, target_url = resolve_evidence_scope(
            question=question,
            app=app,
            flow=flow,
            target_url=target_url,
            state=state,
        )
    except Exception as exc:
        logger.debug("Evidence scope resolve soft-failed: %s", exc)

    session_text = _summarize_analysis_context(state)
    has_session = bool(
        state.get("performance_test_output")
        or state.get("dependencies")
        or state.get("correlations")
        or state.get("transactions")
        or state.get("run_records")
        or state.get("target_url")
        or target_url
    )

    knowledge_text = ""
    if include_knowledge:
        try:
            knowledge_text = _knowledge_context(state, question)
        except Exception as exc:
            logger.debug("Knowledge pack soft-failed: %s", exc)
            knowledge_text = ""

    pack: Dict[str, Any] = {
        "question": question,
        "app": app,
        "flow": flow,
        "target_url": target_url,
        "has_session": has_session,
        "session_json": session_text,
        "knowledge_md": knowledge_text,
        "available_specialists": [
            "knowledge_qa",
            "evidence_trends",
            "integrations",
            "scripting",
        ],
        "evidence_sources": _infer_sources(session_text, knowledge_text, has_session),
    }
    return pack


def slice_for_specialist(
    pack: Dict[str, Any],
    specialist_id: str,
    *,
    max_session_chars: int = 8000,
    max_knowledge_chars: int = 6000,
) -> Dict[str, Any]:
    """Return a trimmed context slice for one specialist."""
    session = str(pack.get("session_json") or "")
    knowledge = str(pack.get("knowledge_md") or "")
    if specialist_id == "knowledge_qa":
        # Prefer knowledge; keep a short session hint
        session = session[:4000]
        knowledge = knowledge[:max_knowledge_chars]
    elif specialist_id == "evidence_trends":
        session = session[:max_session_chars]
        knowledge = knowledge[:4000]
    elif specialist_id == "scripting":
        session = session[:max_session_chars]
        knowledge = knowledge[:2000]
    elif specialist_id == "integrations":
        session = session[:2000]
        knowledge = knowledge[:2000]
    else:
        session = session[:max_session_chars]
        knowledge = knowledge[:max_knowledge_chars]

    return {
        "question": pack.get("question") or "",
        "app": pack.get("app") or "",
        "flow": pack.get("flow") or "",
        "has_session": bool(pack.get("has_session")),
        "session_json": session,
        "knowledge_md": knowledge,
        "evidence_sources": list(pack.get("evidence_sources") or []),
    }


def pack_to_prompt_block(slice_: Dict[str, Any]) -> str:
    """Render a slice as a bounded markdown block for prompts."""
    from src.security.secrets import redact_text_for_llm

    parts: List[str] = [
        f"app={slice_.get('app') or '(none)'} flow={slice_.get('flow') or '(none)'}",
        f"has_session={bool(slice_.get('has_session'))}",
        f"evidence_sources={', '.join(slice_.get('evidence_sources') or []) or '(none)'}",
        "",
        "## Session",
        str(slice_.get("session_json") or "(empty)"),
        "",
        "## Knowledge / RAG",
        str(slice_.get("knowledge_md") or "(empty)"),
    ]
    text = "\n".join(parts)
    text = redact_text_for_llm(text)
    if len(text) > 14000:
        text = text[:14000] + "\n... [truncated]"
    return text


def _infer_sources(session_text: str, knowledge_text: str, has_session: bool) -> List[str]:
    sources: List[str] = []
    if has_session and session_text:
        sources.append("session")
    lower = (knowledge_text or "").lower()
    if "flow card" in lower or "overview" in lower:
        sources.append("knowledge")
    if "rag" in lower or "vector" in lower or "chroma" in lower:
        sources.append("rag")
    if "trend" in lower or "run history" in lower:
        sources.append("knowledge")
    return sources or (["session"] if has_session else [])
