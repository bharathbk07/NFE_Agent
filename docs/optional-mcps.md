## Project MCP servers

All MCP servers used by **NFE_Agent** (the LangGraph app) are defined in one file:

**[`config/mcp_servers.json`](../config/mcp_servers.json)**

This is **not** Cursor IDE MCP (`.cursor/mcp.json`). It is the runtime registry the
Python agents load via `src/tools/mcp_client.py`.

### Enable a server

1. Edit `config/mcp_servers.json`
2. Set `"enabled": true` on the server you need
3. Use `${env:VAR}` for secrets (resolved from `.env` / process env)

```json
{
  "mcpServers": {
    "playwright": {
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"],
      "env": {}
    },
    "my-remote": {
      "enabled": true,
      "transport": "http",
      "url": "https://example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${env:MY_MCP_TOKEN}"
      }
    }
  }
}
```

Optional override path: `MCP_SERVERS_CONFIG=/absolute/path/to/mcp_servers.json`

### Use from code

```python
from src.tools.mcp_client import list_mcp_servers, get_mcp_tools

# Diagnostics
print(list_mcp_servers())              # all configured
print(list_mcp_servers(enabled_only=True))

# LangChain tools for an agent (enabled servers only)
tools = await get_mcp_tools()
# Or only specific servers:
tools = await get_mcp_tools(server_names=["playwright"])
```

Requires: `pip install langchain-mcp-adapters` (listed in `requirements.txt`).

### Defaults in this repo

| Server | Default | Notes |
| --- | --- | --- |
| `k6` | `enabled: false` | Optional Grafana [`k6 x mcp`](https://grafana.com/docs/k6/latest/set-up/configure-ai-assistant/). Smoke/heal uses **CLI** `k6 run` (needed for HTML report JSON). Enable only if you want MCP tools in-bot. |
| `playwright` | `enabled: false` | Pipeline already uses in-process Playwright + CDP |
| `chrome-devtools` | `enabled: false` | Optional live DevTools traces — not the capture layer |

Turn a server on only when a sub-agent should call that MCP at runtime.

### k6 smoke (CLI)

Analysis smoke/heal always runs ``k6 run --out json=...`` via [`src/utils/k6_runner.py`](../src/utils/k6_runner.py) so ``artifacts/k6/html-report.html`` gets full TXN/request tables.

| Env | Effect |
| --- | --- |
| `NFE_K6_MCP=cli` (default) | CLI only (recommended) |
| `NFE_K6_MCP=mcp` / `1` | Try MCP first, then still CLI for the HTML report |
| `NFE_K6_MCP_TIMEOUT` | MCP attempt budget seconds (default `8`) |
| `NFE_K6_MCP_TIMEOUT=8` | Seconds before CLI fallback |

Disable the server with `"enabled": false` on the `k6` entry if you do not want MCP at all.
