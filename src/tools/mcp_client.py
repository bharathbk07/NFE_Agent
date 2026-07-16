"""Load and adapt project MCP server configuration for LangChain clients."""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CONFIG = _PROJECT_ROOT / "config" / "mcp_servers.json"

_ENV_PATTERN = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _interpolate(value: Any) -> Any:
    """Resolve environment placeholders recursively.

    Args:
        value: A scalar, list, or dictionary that may contain ``${env:VAR}``.

    Returns:
        A value of the same shape with string placeholders expanded.
    """
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            """Replace one environment placeholder with its current value.

            Args:
                match: Regular-expression match containing the variable name.

            Returns:
                The environment value, or an empty string when unset.
            """
            return os.getenv(match.group(1), "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    return value


def mcp_config_path() -> Path:
    """Resolve the MCP registry path.

    Returns:
        The absolute configured path, or the project default path.
    """
    override = os.getenv("MCP_SERVERS_CONFIG", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _DEFAULT_CONFIG


def load_mcp_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the unfiltered MCP server registry.

    Args:
        path: Optional registry path; defaults to :func:`mcp_config_path`.

    Returns:
        The decoded JSON object, or an empty ``mcpServers`` mapping if absent.

    Raises:
        json.JSONDecodeError: If the file is not valid JSON.
        OSError: If an existing file cannot be read.
        ValueError: If the JSON root is not an object.
    """
    cfg_path = path or mcp_config_path()
    if not cfg_path.exists():
        logger.warning("MCP config not found at %s", cfg_path)
        return {"mcpServers": {}}
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"MCP config must be a JSON object: {cfg_path}")
    return data


def list_mcp_servers(*, enabled_only: bool = False) -> List[Dict[str, Any]]:
    """Summarize configured MCP servers.

    Args:
        enabled_only: Whether to omit disabled servers.

    Returns:
        Dictionaries containing name, enabled state, transport, and description.
    """
    servers = load_mcp_config().get("mcpServers") or {}
    out: List[Dict[str, Any]] = []
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        enabled = bool(raw.get("enabled", True))
        if enabled_only and not enabled:
            continue
        out.append(
            {
                "name": name,
                "enabled": enabled,
                "transport": raw.get("transport")
                or ("stdio" if raw.get("command") else "http"),
                "description": raw.get("description") or "",
            }
        )
    return out


def get_mcp_connections(
    *,
    enabled_only: bool = True,
    server_names: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Build connections accepted by ``MultiServerMCPClient``.

    Args:
        enabled_only: Whether to omit disabled servers.
        server_names: Optional allowlist of server names.

    Returns:
        A server-name mapping with environment values expanded and project-only
        metadata removed.
    """
    servers = load_mcp_config().get("mcpServers") or {}
    allow = set(server_names) if server_names else None
    connections: Dict[str, Dict[str, Any]] = {}

    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        if allow is not None and name not in allow:
            continue
        if enabled_only and not bool(raw.get("enabled", True)):
            continue

        entry = _interpolate(dict(raw))
        entry.pop("enabled", None)
        entry.pop("description", None)

        # MCP adapters require a transport even when the registry omits it.
        if "transport" not in entry:
            if entry.get("command"):
                entry["transport"] = "stdio"
            elif entry.get("url"):
                # Prefer streamable HTTP; callers can set "sse" explicitly
                entry["transport"] = "http"
            else:
                logger.warning("Skipping MCP server %s: no command or url", name)
                continue

        connections[name] = entry

    return connections


async def get_mcp_client(
    *,
    enabled_only: bool = True,
    server_names: Optional[List[str]] = None,
    tool_name_prefix: bool = True,
):
    """Create a configured multi-server MCP client.

    Args:
        enabled_only: Whether to omit disabled servers.
        server_names: Optional allowlist of server names.
        tool_name_prefix: Whether tool names include their server prefix.

    Returns:
        A ``MultiServerMCPClient``, or ``None`` when no connections remain.

    Raises:
        ImportError: If ``langchain-mcp-adapters`` is unavailable.
    """
    connections = get_mcp_connections(
        enabled_only=enabled_only, server_names=server_names
    )
    if not connections:
        logger.info("No MCP servers enabled in %s", mcp_config_path())
        return None

    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        raise ImportError(
            "langchain-mcp-adapters is required to use project MCP servers. "
            "Install it with: pip install langchain-mcp-adapters"
        ) from exc

    logger.info(
        "Connecting to MCP servers: %s",
        ", ".join(sorted(connections.keys())),
    )
    return MultiServerMCPClient(
        connections,
        tool_name_prefix=tool_name_prefix,
        handle_tool_errors=True,
    )


async def get_mcp_tools(
    *,
    enabled_only: bool = True,
    server_names: Optional[List[str]] = None,
    tool_name_prefix: bool = True,
) -> List[Any]:
    """Load LangChain tools from configured MCP servers.

    Args:
        enabled_only: Whether to omit disabled servers.
        server_names: Optional allowlist of server names.
        tool_name_prefix: Whether tool names include their server prefix.

    Returns:
        A list of LangChain tools; empty when setup or discovery fails.

    Raises:
        ImportError: If connections exist but the MCP adapter is unavailable.
    """
    client = await get_mcp_client(
        enabled_only=enabled_only,
        server_names=server_names,
        tool_name_prefix=tool_name_prefix,
    )
    if client is None:
        return []
    try:
        return await client.get_tools()
    except Exception as exc:
        logger.error("Failed to load MCP tools: %s", exc)
        return []
