"""
Intent routing: decide whether the latest user message is casual conversation,
a question about prior analysis results, a full pipeline run, or a rerun.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal, Optional, Tuple

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from src.utils.model_router import get_model_router, TaskType

logger = logging.getLogger(__name__)

IntentName = Literal[
    "conversation",
    "analysis_qa",
    "performance_analysis",
    "follow_up_analysis",
]

ANALYSIS_KEYWORDS = re.compile(
    r"\b("
    r"performance\s*test|load\s*test|correlation|correlat\w*|corelat\w*|parameteri[sz]ation|"
    r"user\s*journey|navigate|login\s+to|record\s+(the\s+)?(flow|journey)|"
    r"playwright|analyze\s+(this|the|network|traffic)|analyse\s+(this|the|network|traffic)|"
    r"target_url|saucedemo|checkout|add\s+to\s+cart|sub[\s_-]?task|"
    r"run\s+(the\s+)?(analysis|agent|flow|test)|capture\s+traffic|"
    r"extract\s+(dynamic|correlation)|script\s+generation"
    r")\b",
    re.IGNORECASE,
)

RESULT_QA_KEYWORDS = re.compile(
    r"\b("
    r"token|csrf|jwt|bearer|session|cookie|auth(?:entication|enticat\w*)?|"
    r"correlat\w*|corelat\w*|parameter\w*|dynamic\s+value|extract|pass\s+to|"
    r"txn|txns|transaction|transactions|group(?:ing)?\s+request|"
    r"k6|load\s*script|jmeter|gatling|"
    r"why\s+(is|are|was|were|no|not)|is\s+(there|that|this)|do\s+i\s+need|"
    r"needed|missing|not\s+found|no\s+\w+\s+found|what\s+about|explain|"
    r"does\s+(that|this|it)\s+mean|should\s+i|login\s+token|authorization"
    r")\b",
    re.IGNORECASE,
)

FOLLOW_UP_KEYWORDS = re.compile(
    r"\b("
    r"run\s+again|analyze\s+again|analyse\s+again|same\s+(flow|journey|url)|"
    r"previous\s+(flow|journey|run)|retry|re[\s-]?run|do\s+it\s+again|"
    r"use\s+(the\s+)?(last|previous)\s+(one|flow|journey)|"
    r"execute\s+again|replay\s+(the\s+)?(flow|journey)"
    r")\b",
    re.IGNORECASE,
)

GREETING_OR_CHAT = re.compile(
    r"^\s*("
    r"hi|hello|hey|thanks|thank\s+you|ok|okay|bye|good\s*(morning|afternoon|evening)|"
    r"how\s+are\s+you|what('?s|\s+is)\s+up|yo|sup"
    r")[!?.\s]*$",
    re.IGNORECASE,
)

URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
STRUCTURED_KEYS_RE = re.compile(
    r'"(target_url|url|user_journey_steps|journey|credentials|steps)"\s*:',
    re.IGNORECASE,
)


class IntentDecision(BaseModel):
    intent: IntentName = Field(
        description=(
            "conversation = casual chat / math / greetings unrelated to prior analysis; "
            "analysis_qa = question about prior analysis results in this chat (tokens, correlations, params); "
            "performance_analysis = new URL/journey to run the full pipeline; "
            "follow_up_analysis = explicitly rerun the previous journey"
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="0-1 confidence")
    reply: Optional[str] = Field(
        default=None,
        description="Short helpful reply when intent is conversation; otherwise null",
    )
    reason: str = Field(default="", description="Brief reason for the decision")


def get_latest_human_text(messages: Any) -> str:
    """Return plain text of the most recent human message."""
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


def _heuristic_intent(
    text: str,
    has_prior_analysis_context: bool,
) -> Optional[Tuple[IntentName, float, str]]:
    """Fast deterministic routing for obvious cases. Returns None if ambiguous."""
    cleaned = text.strip()
    if not cleaned:
        return "conversation", 0.9, "Empty message"

    if GREETING_OR_CHAT.match(cleaned):
        return "conversation", 0.95, "Greeting / small talk"

    # Explicit rerun of previous journey
    if FOLLOW_UP_KEYWORDS.search(cleaned) and has_prior_analysis_context:
        return "follow_up_analysis", 0.9, "Explicit rerun of prior journey"

    has_url = bool(URL_RE.search(cleaned))
    has_structured = bool(STRUCTURED_KEYS_RE.search(cleaned))
    has_analysis_kw = bool(ANALYSIS_KEYWORDS.search(cleaned))
    has_result_qa = bool(RESULT_QA_KEYWORDS.search(cleaned))

    looks_like_recording = (
        '"type":' in cleaned
        and '"steps"' in cleaned
        and any(tok in cleaned for tok in ('"click"', '"change"', '"navigate"', '"setViewport"'))
    )

    # New journey payload always wins
    if has_url or has_structured or looks_like_recording:
        return "performance_analysis", 0.95, "URL / structured journey payload detected"

    # Questions about prior results — do NOT re-run the pipeline
    if has_prior_analysis_context and (
        has_result_qa
        or (cleaned.endswith("?") and len(cleaned) < 400)
        or has_analysis_kw
    ):
        # "analyze this traffic" without URL while prior exists → QA unless they said run again
        if not FOLLOW_UP_KEYWORDS.search(cleaned):
            return "analysis_qa", 0.9, "Question about prior analysis results"

    # New analysis request without URL yet (will ask for URL in orchestrator)
    if has_analysis_kw and len(cleaned) > 20 and not has_prior_analysis_context:
        return "performance_analysis", 0.8, "Analysis keywords without prior context"

    # Short math / trivia
    if len(cleaned) < 120 and not has_analysis_kw and not has_result_qa and not has_url:
        if re.search(r"\d+\s*[\+\-\*/]\s*\d+", cleaned):
            return "conversation", 0.9, "Math / general question"
        if cleaned.endswith("?") and not has_prior_analysis_context:
            return "conversation", 0.75, "General question without journey signals"

    # Any remaining question with prior analysis context → QA
    if has_prior_analysis_context and ("?" in cleaned or has_result_qa):
        return "analysis_qa", 0.8, "Follow-up with prior analysis available"

    return None


async def classify_intent(
    text: str,
    has_prior_analysis_context: bool = False,
) -> IntentDecision:
    """Classify user intent with heuristics first, LLM only for ambiguous cases."""
    heuristic = _heuristic_intent(text, has_prior_analysis_context)
    if heuristic is not None:
        intent, confidence, reason = heuristic
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
        prompt = f"""You route messages for an NFE performance-testing agent.

Prior analysis results exist in this chat: {has_prior_analysis_context}

User message:
\"\"\"{text[:4000]}\"\"\"

Choose exactly one intent:
- performance_analysis: user provides a NEW URL/journey/recording to run the full browser pipeline.
- follow_up_analysis: user explicitly wants to RERUN the previous journey (e.g. run again).
- analysis_qa: user asks about prior results (tokens, correlations, auth, why something missing, parameters). Prefer this when prior analysis exists and the message is a question.
- conversation: greetings, math, unrelated chit-chat.

If conversation, set reply to a short helpful answer.
"""
        decision = await router.ainvoke_with_failover(
            TaskType.EXTRACTION,
            lambda model: model.with_structured_output(
                IntentDecision, method="json_schema"
            ),
            prompt,
        )
        if isinstance(decision, IntentDecision):
            if decision.intent == "conversation" and not decision.reply:
                decision.reply = _default_conversation_reply(
                    text, has_prior_analysis_context
                )
            return decision
        if isinstance(decision, dict):
            parsed = IntentDecision.model_validate(decision)
            if parsed.intent == "conversation" and not parsed.reply:
                parsed.reply = _default_conversation_reply(
                    text, has_prior_analysis_context
                )
            return parsed
    except Exception as exc:
        logger.warning("LLM intent classification failed (%s); defaulting carefully.", exc)

    if has_prior_analysis_context:
        return IntentDecision(
            intent="analysis_qa",
            confidence=0.6,
            reason="Ambiguous with prior analysis; answering from context",
        )

    return IntentDecision(
        intent="conversation",
        confidence=0.55,
        reply=_default_conversation_reply(text, False),
        reason="Ambiguous; defaulting to conversation",
    )


def _default_conversation_reply(text: str, has_prior: bool = False) -> str:
    cleaned = text.strip()
    if GREETING_OR_CHAT.match(cleaned):
        if has_prior:
            return (
                "Hi! I still have your last performance analysis in this chat.\n\n"
                "Ask about correlations, tokens, parameters, or auth — "
                "or paste a new journey / say **run again** to re-execute."
            )
        return (
            "Hi! I’m the NFE performance-testing agent.\n\n"
            "I can analyze a browser user journey for **parameterization** and **correlation** "
            "(dynamic values between requests).\n\n"
            "To start, send a target URL plus journey steps (or a recording JSON)."
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
            "Ask a specific question about correlations, tokens, or parameters, "
            "or say **run again** to re-run the journey."
        )

    return (
        "I can chat about performance testing here. "
        "To run the specialized agents, provide a **target URL** and **user journey**."
    )


async def route_user_message(
    messages: Any,
    has_prior_analysis_context: bool = False,
) -> IntentDecision:
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
