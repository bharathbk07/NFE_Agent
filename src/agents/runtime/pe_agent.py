"""PE Agent Runtime — OpenClaw-inspired Brain + Hands loop."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.agents.runtime.context_pack import build_context_pack, pack_to_prompt_block
from src.agents.runtime.exec_approval import (
    PendingAction,
    infer_authorizations,
    is_confirm_no,
    is_confirm_yes,
    merge_authorizations,
    needs_confirmation,
)
from src.agents.runtime.hands import build_default_hands
from src.agents.runtime.hands_registry import HandsRegistry
from src.agents.runtime.lane import session_lanes
from src.agents.runtime.memory import append_note, notes_as_context
from src.agents.runtime.skills import catalog_text as skills_catalog
from src.security.secrets import redact_text_for_llm
from src.utils.model_router import TaskType, get_model_router
from src.utils.prompt_loader import render_prompt

logger = logging.getLogger(__name__)


def _content_to_str(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg
    content = getattr(msg, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts)
    return str(msg)


class PEAgentRuntime:
    """Autonomous PE agent: decides which Hands to use for Create/Run/Analyze/Publish."""

    def __init__(self) -> None:
        from config.settings import settings

        self.max_rounds = int(getattr(settings, "NFE_PE_AGENT_MAX_ROUNDS", 10) or 10)

    async def run(self, state: Dict[str, Any], question: str) -> Dict[str, Any]:
        """Run one serialized agent turn.

        Returns dict with answer, tool_calls, pending_action, agent_authorizations, meta.
        """
        thread_id = str(state.get("pe_thread_id") or "default")

        async with session_lanes.acquire(thread_id):
            return await self._run_turn(state, question, thread_id)

    async def _run_turn(
        self, state: Dict[str, Any], question: str, thread_id: str
    ) -> Dict[str, Any]:
        authorizations = merge_authorizations(
            list(state.get("agent_authorizations") or []),
            infer_authorizations(question),
        )
        pending = PendingAction.from_dict(state.get("pending_action"))

        # Resume pending confirmation
        if pending and pending.hand_name:
            if is_confirm_no(question):
                append_note(thread_id, f"User cancelled pending {pending.hand_name}", kind="policy")
                return {
                    "answer": "Okay — cancelled that action. What should I do instead?",
                    "tool_calls": [],
                    "pending_action": None,
                    "agent_authorizations": authorizations,
                    "context_sources": ["agent"],
                    "plan": {"reason": "pending_cancelled"},
                }
            if is_confirm_yes(question) or self._message_selects_pending(question, pending):
                from src.agents.intent_router import ISSUE_KEY_RE

                m = ISSUE_KEY_RE.search(question or "")
                args = dict(pending.args or {})
                if m and "issue_key" in args:
                    args["issue_key"] = m.group(1)
                authorizations = merge_authorizations(
                    authorizations, set(pending.auth_keys or [])
                )
                registry = build_default_hands(state=state)
                result_text, used = await self._invoke_hand(
                    registry, pending.hand_name, args
                )
                follow = await self._continue_after_tool(
                    state,
                    question=(
                        "(Confirmed) Continue after "
                        f"{pending.hand_name}. Original goal may be in memory."
                    ),
                    authorizations=authorizations,
                    thread_id=thread_id,
                    seed_observation=f"Tool {pending.hand_name} result:\n{result_text}",
                    prior_tools=used,
                )
                follow["pending_action"] = None
                follow["agent_authorizations"] = authorizations
                return follow

        # Reliable shortcuts (no LLM) for high-confidence PE asks
        from src.agents.runtime.assist_fastpath import try_assist_fast_path

        fast = await try_assist_fast_path(question, authorizations=authorizations)
        if fast:
            append_note(
                thread_id,
                f"Fast path: {(fast.get('plan') or {}).get('reason')}",
                kind="turn",
            )
            return fast

        pack = build_context_pack(state, question)
        memory_block = notes_as_context(thread_id)
        registry = build_default_hands(state=state)

        # Bind Atlassian MCP tools into the same Brain loop (capability Hands)
        mcp_tools: List[Any] = []
        try:
            from src.agents.runtime.mcp_hands import mcp_tools_catalog_lines
            from src.tools.capability_tools import get_tools_for_capabilities

            mcp_tools = await get_tools_for_capabilities(
                ["jira", "confluence", "alm"]
            )
        except Exception as mcp_exc:
            logger.warning("MCP hands unavailable: %s", mcp_exc)
            mcp_tools = []

        hands_catalog = registry.catalog_text()
        if mcp_tools:
            hands_catalog = (
                hands_catalog
                + "\n\n### MCP Hands (Atlassian)\n"
                + mcp_tools_catalog_lines(mcp_tools)
            )

        system = render_prompt(
            "agents/pe_agent",
            context=pack_to_prompt_block(
                {
                    "question": question,
                    "app": pack.get("app"),
                    "flow": pack.get("flow"),
                    "has_session": pack.get("has_session"),
                    "session_json": pack.get("session_json"),
                    "knowledge_md": (pack.get("knowledge_md") or "")[:6000],
                    "evidence_sources": pack.get("evidence_sources"),
                }
            ),
            skills_catalog=skills_catalog(),
            hands_catalog=hands_catalog,
            authorizations=", ".join(authorizations) or "(none)",
            memory=memory_block or "(empty)",
        )

        tools = list(registry.tools()) + list(mcp_tools)
        tool_map = {getattr(t, "name", ""): t for t in tools}
        messages: List[Any] = [
            SystemMessage(content=system),
            HumanMessage(content=question or ""),
        ]
        tool_names_used: List[str] = []
        router = get_model_router()
        max_rounds = self.max_rounds
        # Cursor SDK is one-shot text — bind_tools does nothing useful and can hang.
        primary = ""
        try:
            primary = router.select_model(TaskType.ASSIST)
        except Exception:
            primary = ""
        use_tools = bool(tools) and not str(primary).startswith("cursor:")

        for _round in range(max(1, max_rounds)):
            ai = await router.ainvoke_with_failover(
                TaskType.ASSIST,
                (lambda model: model.bind_tools(tools)) if use_tools else (lambda model: model),
                messages,
            )
            if not isinstance(ai, AIMessage):
                return {
                    "answer": _content_to_str(ai),
                    "tool_calls": tool_names_used,
                    "pending_action": None,
                    "agent_authorizations": authorizations,
                    "context_sources": ["agent"],
                    "plan": {"reason": "pe_agent_text"},
                }

            messages.append(ai)
            calls = getattr(ai, "tool_calls", None) or []
            if not calls:
                answer = _content_to_str(ai)
                append_note(thread_id, f"Answered: {answer[:240]}", kind="turn")
                return {
                    "answer": answer,
                    "tool_calls": tool_names_used,
                    "pending_action": None,
                    "agent_authorizations": authorizations,
                    "context_sources": ["agent", "tools"]
                    if tool_names_used
                    else ["agent"],
                    "plan": {"reason": "pe_agent_done", "tools": tool_names_used},
                }

            for call in calls:
                name = (
                    call.get("name")
                    if isinstance(call, dict)
                    else getattr(call, "name", "")
                )
                args = (
                    call.get("args")
                    if isinstance(call, dict)
                    else getattr(call, "args", {})
                )
                call_id = (
                    call.get("id")
                    if isinstance(call, dict)
                    else getattr(call, "id", "")
                ) or name
                name = str(name or "")
                args = dict(args or {})
                spec = registry.get(name)
                tool_names_used.append(name)

                if spec and needs_confirmation(spec, authorizations=authorizations):
                    ask = (
                        f"I need your confirmation before **{name}** "
                        f"({spec.risk.value}).\n\n"
                        f"Args: `{json.dumps(args, default=str)[:400]}`\n\n"
                        "Reply **yes** to proceed, **no** to cancel"
                        + (
                            ", or give an issue key if choosing among stories."
                            if "issue" in name or "story" in name
                            else "."
                        )
                    )
                    pending_out = PendingAction(
                        kind="confirm_hand",
                        hand_name=name,
                        args=args,
                        ask=ask,
                        auth_keys=list(spec.auth_keys or []),
                    )
                    return {
                        "answer": ask,
                        "tool_calls": tool_names_used,
                        "pending_action": pending_out.to_dict(),
                        "agent_authorizations": authorizations,
                        "context_sources": ["agent"],
                        "plan": {"reason": "exec_approval_wait", "hand": name},
                    }

                result = await self._run_tool(tool_map.get(name), name, args)
                messages.append(
                    ToolMessage(content=str(result)[:8000], tool_call_id=str(call_id))
                )

        final = await router.ainvoke_with_failover(
            TaskType.ASSIST,
            lambda model: model,
            messages
            + [
                HumanMessage(
                    content=(
                        "Round budget reached. Give the best PE answer now in markdown. "
                        "Do not call tools. State what is still pending if anything."
                    )
                )
            ],
        )
        answer = _content_to_str(final)
        return {
            "answer": answer,
            "tool_calls": tool_names_used,
            "pending_action": None,
            "agent_authorizations": authorizations,
            "context_sources": ["agent", "tools"],
            "plan": {"reason": "pe_agent_max_rounds", "tools": tool_names_used},
        }

    def _message_selects_pending(self, text: str, pending: PendingAction) -> bool:
        from src.agents.intent_router import ISSUE_KEY_RE

        if ISSUE_KEY_RE.search(text or ""):
            return True
        # numeric choice 1/2
        if re_choice(text):
            return True
        return False

    async def _invoke_hand(
        self, registry: HandsRegistry, name: str, args: Dict[str, Any]
    ) -> tuple[str, List[str]]:
        tools = {getattr(t, "name", ""): t for t in registry.tools()}
        result = await self._run_tool(tools.get(name), name, args)
        return result, [name]

    async def _run_tool(self, tool: Any, name: str, args: Dict[str, Any]) -> str:
        if tool is None:
            return json.dumps({"error": f"unknown hand: {name}"})
        try:
            if hasattr(tool, "ainvoke"):
                raw = await tool.ainvoke(args or {})
            else:
                raw = tool.invoke(args or {})
            text = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            return redact_text_for_llm(text)[:8000]
        except Exception as exc:
            logger.warning("Hand %s failed: %s", name, exc)
            return json.dumps({"error": redact_text_for_llm(str(exc))})

    async def _continue_after_tool(
        self,
        state: Dict[str, Any],
        *,
        question: str,
        authorizations: List[str],
        thread_id: str,
        seed_observation: str,
        prior_tools: List[str],
    ) -> Dict[str, Any]:
        """Short follow-up after a confirmed tool — one ASSIST pass with observation."""
        router = get_model_router()
        messages = [
            SystemMessage(
                content=(
                    "You are the NFE PE agent. A risky Hand just ran after user confirmation. "
                    "Summarize the result for the user in clear markdown. "
                    "If the original goal required more steps (e.g. create analysis issue on fail), "
                    "say what you will do next or ask only if still blocked."
                )
            ),
            HumanMessage(content=question),
            HumanMessage(content=seed_observation[:6000]),
        ]
        final = await router.ainvoke_with_failover(
            TaskType.ASSIST, lambda model: model, messages
        )
        answer = _content_to_str(final)
        append_note(thread_id, f"Post-confirm: {answer[:240]}", kind="turn")
        return {
            "answer": answer,
            "tool_calls": prior_tools,
            "context_sources": ["agent", "tools"],
            "plan": {"reason": "pe_agent_post_confirm", "tools": prior_tools},
        }


def re_choice(text: str) -> bool:
    import re

    return bool(re.match(r"^\s*[1-9]\s*$", (text or "").strip()))


async def run_pe_agent(state: Dict[str, Any], question: str) -> Dict[str, Any]:
    """Public entry used by routing / supervisor."""
    from config.settings import settings

    if not getattr(settings, "NFE_PE_AGENT_ENABLED", True):
        from src.agents.runtime.supervisor import PESupervisor

        return await PESupervisor().run(state, question)
    return await PEAgentRuntime().run(state, question)
