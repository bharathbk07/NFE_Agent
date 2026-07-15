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
| `playwright` | `enabled: false` | Pipeline already uses in-process Playwright + CDP |
| `chrome-devtools` | `enabled: false` | Optional live DevTools traces — not the capture layer |

Turn a server on only when a sub-agent should call that MCP at runtime.
