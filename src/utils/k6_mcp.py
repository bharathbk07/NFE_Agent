"""Call Grafana k6 MCP tools from the NFE bot (validate / run).

Configured in ``config/mcp_servers.json`` (``k6 x mcp``). Smoke/heal prefers
MCP when it responds quickly, otherwise falls back to CLI ``k6 run``.

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

# Total budget for MCP validate+run before CLI fallback (seconds).
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


def _mcp_enabled_in_config() -> bool:
    """Return True when the k6 server is enabled in the project MCP registry."""
    try:
        from src.tools.mcp_client import list_mcp_servers

        return any(
            s.get("name") == "k6" and s.get("enabled")
            for s in list_mcp_servers(enabled_only=False)
        )
    except Exception:
        return False


def _want_mcp() -> bool:
    """Whether to attempt k6 MCP (env can force off)."""
    flag = (os.getenv("NFE_K6_MCP") or "auto").strip().lower()
    if flag in ("0", "false", "no", "off", "cli"):
        return False
    if flag in ("1", "true", "yes", "on", "mcp"):
        return True
    # auto
    return _mcp_enabled_in_config()


async def load_k6_mcp_tools() -> List[Any]:
    """Load tools from the project ``k6`` MCP server only (bounded timeout)."""
    global _mcp_disabled_this_process
    if _mcp_disabled_this_process or not _want_mcp():
        return []

    try:
        from src.tools.mcp_client import get_mcp_tools
    except Exception as exc:
        logger.warning("MCP client unavailable: %s", exc)
        _mcp_disabled_this_process = True
        return []

    try:
        tools = await asyncio.wait_for(
            get_mcp_tools(server_names=["k6"], tool_name_prefix=True),
            timeout=min(6.0, _MCP_BUDGET_S),
        )
    except asyncio.TimeoutError:
        logger.warning("k6 MCP tool discovery timed out; CLI for this process")
        _mcp_disabled_this_process = True
        return []
    except Exception as exc:
        logger.warning("Failed to load k6 MCP tools: %s", exc)
        _mcp_disabled_this_process = True
        return []

    if not tools:
        _mcp_disabled_this_process = True
        return []

    logger.info(
        "k6 MCP tools: %s",
        ", ".join(_tool_name(t) for t in tools) or "(none)",
    )
    return tools


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
    attempts = [
        {"script": script_text},
        {"content": script_text},
        {"code": script_text},
        {"path": str(path.resolve())},
        {"file": str(path.resolve())},
    ]
    last_err = ""
    for args in attempts:
        try:
            raw = await asyncio.wait_for(tool.ainvoke(args), timeout=10.0)
            text = _normalize_tool_result(raw)
            lower = text.lower()
            ok = True
            if isinstance(raw, dict) and "ok" in raw:
                ok = bool(raw["ok"])
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
        except Exception as exc:
            last_err = str(exc)
            continue
    logger.warning("k6 MCP validate_script failed: %s", last_err)
    return None


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
    """Run a k6 script via MCP when available."""
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

    from src.utils.k6_runner import _parse_failed_checks, _parse_failed_urls

    script_text = path.read_text(encoding="utf-8")
    attempts = [
        {"script": script_text},
        {"content": script_text},
        {"code": script_text},
        {"path": str(path.resolve())},
        {"file": str(path.resolve())},
        {"script_path": str(path.resolve())},
    ]
    last_err = ""
    for args in attempts:
        try:
            raw = await asyncio.wait_for(tool.ainvoke(args), timeout=90.0)
            text = _normalize_tool_result(raw)
            failed_checks = _parse_failed_checks(text)
            failed_urls = _parse_failed_urls(text)
            lower = text.lower()
            ok = True
            if isinstance(raw, dict) and "ok" in raw:
                ok = bool(raw["ok"])
            elif _nonzero_exit(text):
                ok = False
            elif failed_checks:
                ok = False
            elif "threshold" in lower and "crossed" in lower:
                ok = False
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
            }
        except Exception as exc:
            last_err = str(exc)
            continue
    logger.warning("k6 MCP run_script failed: %s", last_err)
    return None


async def _mcp_smoke_path(script_path: str) -> Optional[Dict[str, Any]]:
    """Full MCP validate→run path; returns None to trigger CLI fallback."""
    tools = await load_k6_mcp_tools()
    if not tools:
        return None

    validated = await mcp_validate_script(script_path, tools=tools)
    if validated is not None and not validated.get("ok"):
        return validated

    mcp_run = await mcp_run_script(script_path, tools=tools)
    if mcp_run is None:
        return None
    if validated and validated.get("ok"):
        mcp_run["validated_via"] = "mcp"
    return mcp_run


async def run_k6_smoke_preferred(script_path: str, **kwargs: Any) -> Dict[str, Any]:
    """Validate + run via k6 MCP when possible; else CLI ``k6 run``.

    Args:
        script_path: Path to the generated ``.js`` script.
        **kwargs: Forwarded to :func:`run_k6_smoke` on CLI fallback.

    Returns:
        Smoke result dictionary (same shape as CLI runner).
    """
    from src.utils.k6_runner import run_k6_smoke

    if _want_mcp():
        try:
            mcp_result = await asyncio.wait_for(
                _mcp_smoke_path(script_path),
                timeout=_MCP_BUDGET_S,
            )
            if mcp_result is not None:
                return mcp_result
        except asyncio.TimeoutError:
            logger.warning(
                "k6 MCP smoke path exceeded %.0fs; falling back to CLI",
                _MCP_BUDGET_S,
            )
        except Exception as exc:
            logger.warning("k6 MCP smoke path error (%s); CLI fallback", exc)

    result = run_k6_smoke(script_path, **kwargs)
    result["via"] = result.get("via") or "cli"
    return result
