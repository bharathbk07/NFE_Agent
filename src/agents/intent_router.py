"""
Intent routing: understand natural-language meaning, then branch the graph.

Mechanical heuristics cover only ultra-clear commands (greetings, URLs,
explicit “work on SCRUM-1”). Everything else goes through the LLM classifier
so product chat does not fire pipelines from keyword hits like “jira”.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional, Tuple

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from src.utils.model_router import get_model_router, TaskType
from src.utils.prompt_loader import render_prompt

logger = logging.getLogger(__name__)

IntentName = Literal[
    "conversation",
    "analysis_qa",
    "pe_assist",
    "performance_analysis",
    "follow_up_analysis",
    "watch_me",
    "reuse_recording",
    "jira_perf",
]

# Product command phrases — used only as *mechanical* short-circuits when the
# whole message is clearly that command, not as substring keyword triggers.
WATCH_ME_COMMAND = re.compile(
    r"^\s*("
    r"watch\s+me|"
    r"record\s+while\s+i(\s+click)?|"
    r"i('?ll| will)\s+click|"
    r"interactive\s+record|"
    r"record\s+my\s+(clicks|actions|flow)|"
    r"i\s+will\s+(drive|navigate)|"
    r"open\s+(a\s+)?browser\s+(and\s+)?(i|let\s+me)"
    r")\b",
    re.IGNORECASE,
)

REUSE_RECORDING_COMMAND = re.compile(
    r"^\s*("
    r"reuse\s+(the\s+)?((last|saved|previous)\s+)?recording|"
    r"analyse?\s+saved\s+recording|"
    r"analyze\s+saved\s+recording|"
    r"load\s+(the\s+)?(saved\s+)?recording|"
    r"use\s+(the\s+)?(last|saved|previous)\s+recording|"
    r"list\s+recordings|"
    r"saved\s+recordings|"
    r"rerun\s+(from\s+)?(saved\s+)?recording|"
    r"replay\s+saved(\s+recording)?"
    r")\b",
    re.IGNORECASE,
)

# Explicit execute + issue key — whole-message style commands only.
JIRA_EXECUTE_WITH_KEY = re.compile(
    r"^\s*("
    r"(please\s+)?"
    r"(work\s+on|process|execute|run)\s+"
    r"((the|a|this)\s+)?"
    r"(jira\s+)?"
    r"(story|issue|ticket)?\s*"
    r"(?P<key>[A-Z][A-Z0-9]+-\d+)"
    r")\s*[!.]?\s*$",
    re.IGNORECASE,
)

JIRA_EXECUTE_NO_KEY = re.compile(
    r"^\s*("
    r"(please\s+)?"
    r"(work\s+on|process|execute)\s+"
    r"((a|the|this)\s+)?"
    r"jira\s+(story|issue|ticket)"
    r"|run\s+jira"
    r")\s*[!.]?\s*$",
    re.IGNORECASE,
)

# List eligible NFE stories via REST (not MCP). Tolerates typos like "stroy".
JIRA_LIST_COMMAND = re.compile(
    r"^\s*("
    r"(please\s+)?"
    r"(list|show|display|get)\s+"
    r"(all\s+|the\s+|my\s+|available\s+|open\s+)?"
    r"(jira\s+)?"
    r"(s(?:tory|tories|troy|troies)|issues?|tickets?)"
    r"|"
    r"what\s+(jira\s+)?(s(?:tory|tories|troy|troies)|issues?|tickets?)\s+"
    r"(are\s+)?(there|available|open|eligible)"
    r")\s*[?.!]?\s*$",
    re.IGNORECASE,
)

FOLLOW_UP_COMMAND = re.compile(
    r"^\s*("
    r"run\s+again|analyze\s+again|analyse\s+again|"
    r"retry|re[\s-]?run|do\s+it\s+again|"
    r"execute\s+again|replay\s+(the\s+)?(flow|journey)|"
    r"same\s+(flow|journey|url)\s+again"
    r")\s*[!.]?\s*$",
    re.IGNORECASE,
)

GREETING_OR_CHAT = re.compile(
    r"^\s*("
    r"hi|hello|hey|thanks|thank\s+you|ok|okay|bye|good\s*(morning|afternoon|evening)|"
    r"how\s+are\s+you|what('?s|\s+is)\s+up|yo|sup"
    r")[!?.\s]*$",
    re.IGNORECASE,
)

ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
STRUCTURED_KEYS_RE = re.compile(
    r'"(target_url|url|user_journey_steps|journey|credentials|steps)"\s*:',
    re.IGNORECASE,
)

# Soft signals for the LLM prompt only (never used as hard routing).
_SOFT_WATCH = re.compile(r"\b(watch\s+me|record\s+while|i('?ll| will)\s+click)\b", re.I)
_SOFT_JIRA = re.compile(r"\b(jira|scrum-\d+|[A-Z][A-Z0-9]+-\d+)\b", re.I)
_SOFT_JIRA_LIST = re.compile(
    r"\b(list|show|display)\b.*\b(jira|stor\w*|issues?|tickets?)\b"
    r"|\b(jira|stories|issues)\b.*\b(list|available|open)\b",
    re.I,
)
_SOFT_REUSE = re.compile(r"\b(saved\s+recording|list\s+recordings|reuse\s+recording)\b", re.I)
_SOFT_QA = re.compile(
    r"\b(why|what|how|explain|trend|smoke|p95|script|result|fail|token|csrf)\b",
    re.I,
)
_SOFT_RERUN = re.compile(r"\b(run\s+again|retry|re[\s-]?run)\b", re.I)
_QUESTIONISH = re.compile(
    r"^\s*(what|why|how|when|where|which|who|is|are|was|were|did|does|do|can|could|"
    r"should|would|tell\s+me|explain|summarize|summarise)\b|\?\s*$",
    re.I,
)


class IntentDecision(BaseModel):
    """A validated routing decision for the latest user message."""

    intent: IntentName = Field(
        description=(
            "conversation = greetings / math only; "
            "pe_assist = personal PE assistant (supervisor + specialists); "
            "analysis_qa = legacy alias for pe_assist; "
            "watch_me = user will click in a headed browser the bot opens; "
            "reuse_recording = load/list a saved Watch-me recording; "
            "jira_perf = user explicitly asks to EXECUTE a Jira issue workflow; "
            "performance_analysis = new URL/journey to run the full pipeline; "
            "follow_up_analysis = explicitly rerun the previous journey"
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence")
    reply: Optional[str] = Field(
        default=None,
        description="Short helpful reply when intent is conversation; otherwise null",
    )
    reason: str = Field(default="", description="Brief reason citing user meaning")


def get_latest_human_text(messages: Any) -> str:
    """Extract text from the most recent human chat message.

    Args:
        messages: Iterable of LangChain messages, potentially with content blocks.

    Returns:
        Stripped plain text, or an empty string when no human message exists.
    """
    if not messages:
        return ""
    for msg in reversed(list(messages)):
        if not isinstance(msg, HumanMessage):
            continue
        content = msg.content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
            return "\n".join(parts).strip()
        if content is None:
            return ""
        return str(content).strip()
    return ""


def _soft_signals(text: str, has_prior: bool) -> str:
    """Build non-binding hint lines for the LLM classifier prompt."""
    hints = []
    if _SOFT_WATCH.search(text):
        hints.append("- message mentions interactive-recording language")
    if _SOFT_JIRA.search(text):
        hints.append(
            "- message mentions Jira / an issue key (topic ≠ execute unless asked)"
        )
    if _SOFT_JIRA_LIST.search(text):
        hints.append(
            "- message may ask to list eligible Jira stories (use jira_perf list mode)"
        )
    if _SOFT_REUSE.search(text):
        hints.append("- message mentions saved/list recordings")
    if _SOFT_QA.search(text):
        hints.append("- message looks question/explanation oriented")
    if _SOFT_RERUN.search(text):
        hints.append("- message may ask to rerun")
    if _QUESTIONISH.search(text):
        hints.append("- message reads as a question")
    if URL_RE.search(text):
        hints.append("- message contains a URL")
    if has_prior:
        hints.append("- prior analysis/smoke/script context is available in this chat")
    else:
        hints.append("- no prior analysis context in this chat")
    if not hints:
        return "- (none)"
    return "\n".join(hints)


def _mechanical_intent(
    text: str,
    has_prior_analysis_context: bool,
) -> Optional[Tuple[IntentName, float, str]]:
    """Route only ultra-clear mechanical commands without an LLM.

    Natural-language messages return ``None`` so the classifier understands
    meaning. Keyword *topics* (jira, smoke, k6, …) never short-circuit here.

    Args:
        text: Latest user message.
        has_prior_analysis_context: Whether reusable analysis state exists.

    Returns:
        An intent, confidence, and reason tuple, or ``None`` when ambiguous.
    """
    cleaned = text.strip()
    if not cleaned:
        return "conversation", 0.9, "Empty message"

    if GREETING_OR_CHAT.match(cleaned):
        return "conversation", 0.95, "Greeting / small talk"

    # Pure arithmetic — keep free of LLM cost
    if len(cleaned) < 80 and re.fullmatch(
        r"\s*\d+\s*[\+\-\*/]\s*\d+\s*[?.!]?\s*", cleaned
    ):
        return "conversation", 0.9, "Math expression"

    # List eligible stories → PE assistant (Integrations specialist), not execute worker
    if JIRA_LIST_COMMAND.match(cleaned):
        return "pe_assist", 0.96, "Explicit Jira list → PE assistant"

    # Explicit whole-message Jira *execute* commands only
    if JIRA_EXECUTE_WITH_KEY.match(cleaned) or JIRA_EXECUTE_NO_KEY.match(cleaned):
        if not _QUESTIONISH.search(cleaned):
            return "jira_perf", 0.96, "Explicit Jira execute command"

    # Explicit whole-message reuse / watch-me / rerun commands
    if REUSE_RECORDING_COMMAND.match(cleaned):
        return "reuse_recording", 0.95, "Explicit reuse/list recording command"

    if WATCH_ME_COMMAND.match(cleaned):
        if URL_RE.search(cleaned):
            return "watch_me", 0.96, "Explicit watch-me command with URL"
        # "watch me" alone is still a clear product command
        if len(cleaned) < 160:
            return "watch_me", 0.9, "Explicit watch-me command"

    if FOLLOW_UP_COMMAND.match(cleaned) and has_prior_analysis_context:
        return "follow_up_analysis", 0.92, "Explicit rerun command"

    # Machine payloads (URL / JSON journey) — not free-form NL
    has_url = bool(URL_RE.search(cleaned))
    has_structured = bool(STRUCTURED_KEYS_RE.search(cleaned))
    looks_like_recording = (
        '"type":' in cleaned
        and '"steps"' in cleaned
        and any(
            tok in cleaned
            for tok in ('"click"', '"change"', '"navigate"', '"setViewport"')
        )
    )
    if has_structured or looks_like_recording:
        return "performance_analysis", 0.95, "Structured journey / recording payload"
    # Bare URL-only paste (no question language) → new analysis
    if has_url and not _QUESTIONISH.search(cleaned) and len(cleaned) < 300:
        # If they also said watch-me mid-sentence, let LLM decide
        if not _SOFT_WATCH.search(cleaned) and not _SOFT_JIRA.search(cleaned):
            return "performance_analysis", 0.9, "URL payload for new analysis"

    return None


def wants_jira_list(text: str) -> bool:
    """True when the user asks to list eligible Jira stories (not execute)."""
    return bool(JIRA_LIST_COMMAND.match((text or "").strip()))


def _wants_jira_perf(text: str) -> bool:
    """True for explicit Jira *execute* commands (mechanical path)."""
    cleaned = (text or "").strip()
    if wants_jira_list(cleaned):
        return False
    if _QUESTIONISH.search(cleaned):
        return False
    return bool(
        JIRA_EXECUTE_WITH_KEY.match(cleaned) or JIRA_EXECUTE_NO_KEY.match(cleaned)
    )


def _promote_to_pe_assist(decision: IntentDecision, text: str) -> IntentDecision:
    """Map open PE chat onto the supervisor path (not canned conversation)."""
    if decision.intent in ("pe_assist", "analysis_qa"):
        return IntentDecision(
            intent="pe_assist",
            confidence=decision.confidence,
            reply=None,
            reason=decision.reason or "pe_assist",
        )
    if decision.intent != "conversation":
        return decision
    cleaned = (text or "").strip()
    if GREETING_OR_CHAT.match(cleaned):
        return decision
    if len(cleaned) < 80 and re.fullmatch(
        r"\s*\d+\s*[\+\-\*/]\s*\d+\s*[?.!]?\s*", cleaned
    ):
        return decision
    return IntentDecision(
        intent="pe_assist",
        confidence=max(decision.confidence, 0.7),
        reply=None,
        reason=f"Promoted conversation → pe_assist ({decision.reason})",
    )


# Back-compat alias used by older tests / imports
_heuristic_intent = _mechanical_intent


async def classify_intent(
    text: str,
    has_prior_analysis_context: bool = False,
) -> IntentDecision:
    """Classify user intent with LLM understanding; mechanical fallback only.

    Args:
        text: Latest user message as plain text.
        has_prior_analysis_context: Whether prior pipeline results are available.

    Returns:
        A validated intent decision and optional conversational reply.
    """
    mechanical = _mechanical_intent(text, has_prior_analysis_context)
    if mechanical is not None:
        intent, confidence, reason = mechanical
        reply = None
        if intent == "conversation":
            reply = _default_conversation_reply(text, has_prior_analysis_context)
        return IntentDecision(
            intent=intent,
            confidence=confidence,
            reply=reply,
            reason=reason,
        )

    try:
        router = get_model_router()
        prompt = render_prompt(
            "intent_classifier",
            has_prior_analysis_context=has_prior_analysis_context,
            user_message=text[:4000],
            soft_signals=_soft_signals(text, has_prior_analysis_context),
        )
        decision = await router.ainvoke_with_failover(
            TaskType.EXTRACTION,
            lambda model: model.with_structured_output(
                IntentDecision, method="json_schema"
            ),
            prompt,
        )
        if isinstance(decision, IntentDecision):
            decision = _guard_pipeline_intents(decision, text, has_prior_analysis_context)
            decision = _promote_to_pe_assist(decision, text)
            if decision.intent == "conversation" and not decision.reply:
                decision.reply = _default_conversation_reply(
                    text, has_prior_analysis_context
                )
            return decision
        if isinstance(decision, dict):
            parsed = IntentDecision.model_validate(decision)
            parsed = _guard_pipeline_intents(parsed, text, has_prior_analysis_context)
            parsed = _promote_to_pe_assist(parsed, text)
            if parsed.intent == "conversation" and not parsed.reply:
                parsed.reply = _default_conversation_reply(
                    text, has_prior_analysis_context
                )
            return parsed
    except Exception as exc:
        logger.warning("LLM intent classification failed (%s); defaulting carefully.", exc)

    # Fail closed: never auto-run Jira/pipeline on ambiguity — use PE assistant
    return IntentDecision(
        intent="pe_assist",
        confidence=0.55,
        reason="Ambiguous; defaulting to pe_assist (fail-closed)",
    )


def _guard_pipeline_intents(
    decision: IntentDecision,
    text: str,
    has_prior: bool,
) -> IntentDecision:
    """Downgrade risky pipeline intents when the message is clearly a question.

    Prevents selling a brittle product where “jira” / issue keys in a question
    accidentally start the Jira worker.
    """
    if decision.intent not in ("jira_perf", "performance_analysis", "follow_up_analysis"):
        return decision
    # Listing stories → PE assistant (not jira execute worker)
    if decision.intent == "jira_perf" and (
        wants_jira_list(text) or _SOFT_JIRA_LIST.search(text or "")
    ):
        return IntentDecision(
            intent="pe_assist",
            confidence=decision.confidence,
            reply=None,
            reason="List Jira → pe_assist (Integrations specialist)",
        )
    if not _QUESTIONISH.search(text or ""):
        return decision
    # Question + prior context → PE assist; question without prior → PE assist too
    return IntentDecision(
        intent="pe_assist",
        confidence=min(decision.confidence, 0.75),
        reply=None,
        reason=(
            f"Guarded: message is a question; refused pipeline intent "
            f"'{decision.intent}' ({decision.reason})"
        ),
    )


def _default_conversation_reply(text: str, has_prior: bool = False) -> str:
    """Generate a deterministic reply for non-pipeline conversation.

    Args:
        text: Latest user message.
        has_prior: Whether an earlier analysis can be referenced.

    Returns:
        A short conversational, arithmetic, or capability response.
    """
    cleaned = text.strip()
    if GREETING_OR_CHAT.match(cleaned):
        if has_prior:
            return (
                "Hi! I still have your last performance analysis in this chat.\n\n"
                "Ask about correlations, tokens, parameters, smoke, or trends — "
                "or paste a new journey / say **run again** to re-execute.\n"
                "To process a Jira story, say clearly: **work on SCRUM-1**."
            )
        return (
            "Hi! I’m the NFE performance-testing agent.\n\n"
            "I can analyze a browser user journey for **parameterization** and **correlation** "
            "(dynamic values between requests).\n\n"
            "To start:\n"
            "- Send a target URL plus journey steps, **or**\n"
            "- Say **watch me** with a URL — I’ll open a browser; click through, then **Done recording**.\n"
            "- Later: **analyse saved recording** (no re-record) or **list recordings**.\n"
            "- Jira: say **work on SCRUM-1** only when you want me to *run* that story."
        )

    math = re.search(r"(\d+)\s*([\+\-\*/])\s*(\d+)", cleaned)
    if math:
        a, op, b = int(math.group(1)), math.group(2), int(math.group(3))
        try:
            result = {"+": a + b, "-": a - b, "*": a * b, "/": (a / b if b else "undefined")}[op]
            return f"{a} {op} {b} = **{result}**"
        except Exception:
            pass

    if has_prior:
        return (
            "I still have your previous analysis in context. "
            "Ask a specific question about results, scripts, or correlations, "
            "or say **run again** / **work on SCRUM-1** only if you want a new run."
        )

    return (
        "I can help with performance testing here. "
        "Tell me what you want to do — analyse a URL, **watch me** record a flow, "
        "reuse a saved recording, or **work on SCRUM-1** to execute a Jira story. "
        "Mentioning a tool name alone won’t start a run."
    )


async def route_user_message(
    messages: Any,
    has_prior_analysis_context: bool = False,
) -> IntentDecision:
    """Route the latest human message into an NFE graph intent.

    Args:
        messages: Conversation messages in LangChain-compatible form.
        has_prior_analysis_context: Whether prior analysis state exists.

    Returns:
        The validated routing decision for the latest human message.
    """
    text = get_latest_human_text(messages)
    decision = await classify_intent(
        text, has_prior_analysis_context=has_prior_analysis_context
    )
    logger.info(
        "Intent routed to '%s' (confidence=%.2f, reason=%s)",
        decision.intent,
        decision.confidence,
        decision.reason,
    )
    return decision
