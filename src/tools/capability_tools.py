"""Capability-based MCP tool binding for PE specialists."""
from __future__ import annotations

import fnmatch
import logging
from typing import Any, Dict, List, Optional, Sequence, Set

from src.tools.mcp_client import load_mcp_config

logger = logging.getLogger(__name__)

# Cache discovered tools per process: capability frozenset -> tools
_TOOL_CACHE: Dict[str, List[Any]] = {}


def server_capability_index() -> Dict[str, Dict[str, Any]]:
    """Return enabled MCP servers with capability/safety metadata."""
    servers = load_mcp_config().get("mcpServers") or {}
    out: Dict[str, Dict[str, Any]] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        if not bool(raw.get("enabled", False)):
            continue
        caps = raw.get("capabilities") or []
        if isinstance(caps, str):
            caps = [caps]
        out[name] = {
            "capabilities": [str(c).lower() for c in caps],
            "tool_allowlist": list(raw.get("tool_allowlist") or ["*"]),
            "tool_denylist": list(raw.get("tool_denylist") or []),
            "timeout_s": float(raw.get("timeout_s") or 20),
            "read_only": bool(raw.get("read_only", True)),
        }
    return out


def servers_for_capabilities(accepted: Sequence[str]) -> List[str]:
    """Server names whose capabilities intersect ``accepted``."""
    want = {a.lower() for a in accepted}
    names = []
    for name, meta in server_capability_index().items():
        if want.intersection(meta["capabilities"]):
            names.append(name)
    return names


def _name_allowed(tool_name: str, meta: Dict[str, Any]) -> bool:
    name = tool_name or ""
    for pat in meta.get("tool_denylist") or []:
        if fnmatch.fnmatch(name.lower(), str(pat).lower()):
            return False
    allow = meta.get("tool_allowlist") or ["*"]
    return any(fnmatch.fnmatch(name.lower(), str(pat).lower()) for pat in allow)


def _looks_write_tool(name: str) -> bool:
    lower = (name or "").lower()
    return any(
        tok in lower
        for tok in (
            "delete",
            "create",
            "update",
            "transition",
            "write",
            "post",
            "put",
            "patch",
            "remove",
            "edit",
        )
    )


async def get_tools_for_capabilities(
    accepted: Sequence[str],
    *,
    allow_writes: bool = False,
) -> List[Any]:
    """Load allowlisted MCP tools for specialists that accept ``accepted`` caps.

    Fail-soft: returns [] on discovery errors.
    """
    cache_key = ",".join(sorted({a.lower() for a in accepted})) + f"|w={allow_writes}"
    if cache_key in _TOOL_CACHE:
        return list(_TOOL_CACHE[cache_key])

    server_names = servers_for_capabilities(accepted)
    if not server_names:
        return []

    try:
        from src.tools.mcp_client import get_mcp_tools

        raw_tools = await get_mcp_tools(
            enabled_only=True,
            server_names=server_names,
            tool_name_prefix=True,
        )
    except Exception as exc:
        logger.warning("MCP capability bind failed: %s", exc)
        return []

    index = server_capability_index()
    filtered: List[Any] = []
    for tool in raw_tools or []:
        name = getattr(tool, "name", "") or ""
        # Prefix is usually server_tool
        server = name.split("_", 1)[0] if "_" in name else ""
        meta = index.get(server) or next(iter(index.values()), {})
        if not _name_allowed(name, meta):
            continue
        if meta.get("read_only", True) and not allow_writes and _looks_write_tool(name):
            continue
        filtered.append(tool)

    _TOOL_CACHE[cache_key] = filtered
    logger.info(
        "MCP tools bound for caps=%s servers=%s count=%s",
        list(accepted),
        server_names,
        len(filtered),
    )
    return list(filtered)


def clear_mcp_tool_cache() -> None:
    """Clear process MCP tool cache (tests)."""
    _TOOL_CACHE.clear()
