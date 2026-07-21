"""Optional Grafana k6 MCP helpers (validate / run).

Smoke/heal in the analysis pipeline uses CLI ``k6 run`` (needed for
``--out json`` + HTML report). MCP is opt-in via ``NFE_K6_MCP=mcp`` and is
disabled after the first BrokenResourceError / TaskGroup failure.

See https://grafana.com/docs/k6/latest/set-up/configure-ai-assistant/
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MCP_BUDGET_S = float(os.getenv("NFE_K6_MCP_TIMEOUT", "8"))
_mcp_disabled_this_process = False


def _tool_name(tool: Any) -> str:
    """Return a lowercase tool identifier."""
    return str(getattr(tool, "name", "") or "").lower()


def _pick_tool(tools: List[Any], *needles: str) -> Any:
    """Find the first tool whose name contains all needles."""
    for tool in tools:
        name = _tool_name(tool)
        if all(n in name for n in needles):
            return tool
    return None


def _normalize_tool_result(raw: Any) -> str:
    """Flatten MCP / LangChain tool output to text."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if content is not None:
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(str(block))
            return "\n".join(parts)
        return str(content)
    if isinstance(raw, dict):
        return json.dumps(raw, default=str)
    return str(raw)


def _disable_mcp(reason: str) -> None:
    """Stop further MCP attempts for this process (stdio often stays broken)."""
    global _mcp_disabled_this_process
    _mcp_disabled_this_process = True
    logger.warning("k6 MCP disabled for this process: %s", reason)


def _is_stdio_breakage(exc: BaseException) -> bool:
    """Return True for MCP stdio teardown / TaskGroup failures."""
    name = type(exc).__name__
    if name in ("BrokenResourceError", "ExceptionGroup", "BaseExceptionGroup"):
        return True
    text = str(exc).lower()
    return (
        "brokenresource" in text
        or "taskgroup" in text
        or "unhandled errors in a taskgroup" in text
    )


def _want_mcp() -> bool:
    """Whether to attempt k6 MCP (opt-in only).

    Default / ``cli`` / ``auto`` → False. Set ``NFE_K6_MCP=mcp`` (or ``1``)
    to try MCP before CLI.
    """
    flag = (os.getenv("NFE_K6_MCP") or "cli").strip().lower()
    return flag in ("1", "true", "yes", "on", "mcp")


async def load_k6_mcp_tools() -> List[Any]:
    """Load tools from the project ``k6`` MCP server only (bounded timeout)."""
    global _mcp_disabled_this_process
    if _mcp_disabled_this_process or not _want_mcp():
        return []

    try:
        from src.tools.mcp_client import get_mcp_tools
    except Exception as exc:
        _disable_mcp(f"client unavailable ({exc})")
        return []

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools(server_names=["k6"], tool_name_prefix=True),
            timeout=min(6.0, _MCP_BUDGET_S),
        )
    except asyncio.TimeoutError:
        _disable_mcp("tool discovery timed out")
        return []
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        _disable_mcp(f"tool discovery failed ({type(exc).__name__}: {exc})")
        return []

    if not tools:
        _disable_mcp("no tools returned")
        return []

    logger.info(
        "k6 MCP tools: %s",
        ", ".join(_tool_name(t) for t in tools) or "(none)",
    )
    return tools


async def _ainvoke_script(tool: Any, script_text: str, *, timeout: float) -> Any:
    """Invoke an MCP tool with ``script`` only; swallow stdio breakages."""
    try:
        return await asyncio.wait_for(
            tool.ainvoke({"script": script_text}),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        _disable_mcp(f"ainvoke timed out after {timeout:.0f}s")
        raise
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            raise
        if _is_stdio_breakage(exc):
            _disable_mcp(f"stdio broken ({type(exc).__name__})")
            return None
        _disable_mcp(f"ainvoke failed ({type(exc).__name__}: {exc})")
        return None


async def mcp_validate_script(
    script_path: str, tools: Optional[List[Any]] = None
) -> Optional[Dict[str, Any]]:
    """Validate a k6 script via MCP when available."""
    path = Path(script_path)
    if not path.is_file():
        return {
            "ok": False,
            "via": "mcp-validate",
            "stdout": "",
            "stderr": f"Script not found: {script_path}",
            "summary": "script missing",
            "failed_checks": [],
            "failed_urls": [],
            "exit_code": -1,
            "skipped": False,
        }

    tools = tools if tools is not None else await load_k6_mcp_tools()
    tool = _pick_tool(tools, "validate")
    if tool is None:
        return None

    script_text = path.read_text(encoding="utf-8")
    try:
        raw = await _ainvoke_script(tool, script_text, timeout=10.0)
    except asyncio.TimeoutError:
        return None
    if raw is None:
        return None

    text = _normalize_tool_result(raw)
    lower = text.lower()
    ok = True
    if isinstance(raw, dict) and "ok" in raw:
        ok = bool(raw["ok"])
    elif isinstance(raw, dict) and "valid" in raw:
        ok = bool(raw["valid"])
    elif any(
        tok in lower
        for tok in (
            "syntaxerror",
            "invalid script",
            "validation failed",
            "parse error",
        )
    ):
        ok = False
    elif "error" in lower and "valid" not in lower:
        ok = False
    return {
        "ok": ok,
        "via": "mcp-validate",
        "stdout": text[-8000:],
        "stderr": "" if ok else text[-4000:],
        "summary": "validated" if ok else "validate failed",
        "failed_checks": [],
        "failed_urls": [],
        "exit_code": 0 if ok else 1,
        "skipped": False,
    }


def _nonzero_exit(text: str) -> bool:
    """Return True if text suggests a non-zero k6 exit code."""
    m = re.search(r"exit(?:\s+code)?[:\s]+(\d+)", text, re.IGNORECASE)
    if not m:
        return False
    try:
        return int(m.group(1)) != 0
    except ValueError:
        return False


async def mcp_run_script(
    script_path: str, tools: Optional[List[Any]] = None
) -> Optional[Dict[str, Any]]:
    """Run a k6 script via MCP when available (no HTML points export)."""
    path = Path(script_path)
    if not path.is_file():
        return {
            "ok": False,
            "via": "mcp-run",
            "stdout": "",
            "stderr": f"Script not found: {script_path}",
            "summary": "script missing",
            "failed_checks": [],
            "failed_urls": [],
            "exit_code": -1,
            "skipped": False,
        }

    tools = tools if tools is not None else await load_k6_mcp_tools()
    tool = _pick_tool(tools, "run") or _pick_tool(tools, "execute")
    if tool is None:
        return None

    from src.utils.k6_html_report import find_html_report, report_paths_for_script
    from src.utils.k6_runner import _parse_failed_checks, _parse_failed_urls

    report_paths = report_paths_for_script(path)
    os.environ["NFE_K6_HTML_REPORT"] = report_paths["html"]
    os.environ["NFE_K6_SUMMARY_JSON"] = report_paths["json"]

    script_text = path.read_text(encoding="utf-8")
    try:
        raw = await _ainvoke_script(tool, script_text, timeout=90.0)
    except asyncio.TimeoutError:
        return None
    if raw is None:
        return None

    text = _normalize_tool_result(raw)
    failed_checks = _parse_failed_checks(text)
    failed_urls = _parse_failed_urls(text)
    lower = text.lower()
    ok = True
    if isinstance(raw, dict):
        if "ok" in raw:
            ok = bool(raw["ok"])
        elif "success" in raw:
            ok = bool(raw["success"])
        elif "exit_code" in raw:
            try:
                ok = int(raw["exit_code"]) == 0
            except (TypeError, ValueError):
                pass
    if ok and _nonzero_exit(text):
        ok = False
    if ok and failed_checks:
        ok = False
    if ok and "threshold" in lower and "crossed" in lower:
        ok = False
    html_report = find_html_report(path) or ""
    summary_json = (
        report_paths["json"] if Path(report_paths["json"]).is_file() else ""
    )
    return {
        "ok": ok,
        "via": "mcp-run",
        "stdout": text[-8000:],
        "stderr": "" if ok else text[-4000:],
        "summary": "passed" if ok else "failed (mcp run)",
        "failed_checks": failed_checks[:40],
        "failed_urls": failed_urls[:40],
        "exit_code": 0 if ok else 1,
        "skipped": False,
        "html_report": html_report,
        "summary_json": summary_json,
    }


async def run_k6_smoke_preferred(script_path: str, **kwargs: Any) -> Dict[str, Any]:
    """Run smoke via CLI ``k6 run`` only (HTML report + JSON points).

    MCP is not used on this path: it cannot emit ``--out json`` points for
    TXN/request tables and frequently crashes the stdio session.

    Args:
        script_path: Path to the generated ``.js`` script.
        **kwargs: Forwarded to :func:`run_k6_smoke`.

    Returns:
        Smoke result dictionary with ``via=cli``.
    """
    from src.utils.k6_runner import run_k6_smoke

    result = run_k6_smoke(script_path, **kwargs)
    result["via"] = "cli"
    return result
