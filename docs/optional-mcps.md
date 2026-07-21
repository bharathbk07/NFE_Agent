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
| `k6` | `enabled: true` | Grafana [`k6 x mcp`](https://grafana.com/docs/k6/latest/set-up/configure-ai-assistant/) — validate/run for smoke + heal. Needs **k6 v2+** on `PATH`. |
| `playwright` | `enabled: false` | Pipeline already uses in-process Playwright + CDP |
| `chrome-devtools` | `enabled: false` | Optional live DevTools traces — not the capture layer |

Turn a server on only when a sub-agent should call that MCP at runtime.

### k6 MCP (recommended)

The analysis pipeline prefers Grafana k6 MCP tools (`validate_script` / `run_script`) via [`src/utils/k6_mcp.py`](../src/utils/k6_mcp.py), then falls back to CLI `k6 run`. First-time `k6 x mcp` may download an extension binary (needs network).

```bash
# Confirm k6 supports the MCP subcommand (k6 v2+)
k6 x mcp --help
```

| Env | Effect |
| --- | --- |
| `NFE_K6_MCP=auto` (default) | Try MCP when enabled in config; CLI on timeout/error |
| `NFE_K6_MCP=0` / `cli` | Force CLI smoke only |
| `NFE_K6_MCP=1` | Prefer MCP |
| `NFE_K6_MCP_TIMEOUT=8` | Seconds before CLI fallback |

Disable the server with `"enabled": false` on the `k6` entry if you do not want MCP at all.
