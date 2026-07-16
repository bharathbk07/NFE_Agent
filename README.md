# Browser Flow Analyzer Agent

An agentic system built with LangGraph and LangChain that translates natural language user journeys into Playwright automation scripts, executes them across isolated browser contexts, and performs differential network analysis to discover dynamic correlation values (like tokens, authorization headers, session state, etc.) and map request/response dependencies.

Network capture uses **Chrome DevTools Protocol (CDP)** via Playwright (`Network.requestWillBeSent` / `responseReceived` / `getResponseBody`) so parameterization, correlation, and transaction grouping see DevTools-grade request detail.

Optional MCP servers for the **app** (not Cursor) are managed in one file: [`config/mcp_servers.json`](config/mcp_servers.json) — see [`docs/optional-mcps.md`](docs/optional-mcps.md).

Load-test scripting uses a **deterministic IR → k6** path (no LLM for script generation): capture → params/correlations/TXNs → `load_test_ir` → `k6_script`.

---

## 🛠️ Setup & Execution

### 1. Installation
Clone the repository, initialize your virtual environment, and install dependencies:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install
```

### 2. Environment Configuration
Create a `.env` file in the project root:
```ini
GEMINI_API_KEY="your-gemini-api-key"
GEMINI_MODEL="gemini-2.5-flash"

# Multi-model auto-routing (optional)
# LLM_MODELS=google:gemini-3.1-flash-lite,google:gemini-3.5-flash,cursor:composer-2.5
# LLM_TASK_ROUTING={"orchestration":"cursor:composer-2.5","extraction":"google:gemini-3.1-flash-lite"}

# Cursor AI (optional — native cursor-sdk, orchestration/navigation only)
# CURSOR_API_KEY="crsr_..."
# CURSOR_RUNTIME="local"
# CURSOR_CLOUD_REPO="https://github.com/your-org/NFE_Agent"

# LangSmith Configuration (Optional, for Prompt Registry & Traces)
LANGCHAIN_TRACING_V2="true"
LANGCHAIN_API_KEY="your-langsmith-api-key"
LANGCHAIN_PROJECT="nfe-agent"

# Dynatrace / OpenTelemetry Integration (Optional)
TRACELOOP_BASE_URL="your-dynatrace-otlp-endpoint"
TRACELOOP_HEADERS="Authorization=Api-Token your-token"
```

---

## 📝 Prompt Versioning System

The system decouples agent prompts from the Python code to enable version control and rapid iteration. The prompt configuration uses a dual-layer strategy:

1. **LangSmith Prompt Registry (Production)**:
   If `LANGCHAIN_TRACING_V2` and `LANGCHAIN_API_KEY` are configured in `.env`, the agent will attempt to pull the prompt named `navigator_agent_step_planner` dynamically from your LangSmith registry. This allows you to update and version prompts live in LangSmith without redeploying code.
2. **Local Versioned Fallback (Development & Offline)**:
   If LangSmith is unavailable, offline, or returns an error, the agent falls back to loading [prompts/navigator_agent_step_planner.txt](file:///Users/bk/Projects/NFE_Agent/prompts/navigator_agent_step_planner.txt). This file is fully tracked in Git, ensuring version history is kept inline with the codebase.

---

## 🖥️ Invoking the LangGraph Studio UI

The agent is fully compatible with **LangGraph Studio**, providing a visual interface to trigger the agent, step through execution nodes, and inspect network logs/correlation reports.

### Start the Local Dev Server
Ensure you have the LangGraph CLI installed, then start the development server:
```bash
pip install langgraph-cli
langgraph dev
```
Once started, the CLI will output a local URL (typically `http://localhost:2024` or similar). Open this URL in your web browser or load the directory in the desktop version of **LangGraph Studio** to interact with the graph.

---

## 💡 Prompting in the UI for Best Results

When running the agent in LangGraph Studio, the graph expects input variables: `target_url`, `credentials`, and `user_journey_steps` (the description). Follow these prompting strategies in the UI input fields to get optimized and accurate browser planning:

### 1. Structure the Inputs Properly
Provide clean JSON inputs for the graph parameters:
*   **`target_url`**: The absolute starting URL of the journey (e.g. `https://example.com/login`).
*   **`credentials`**: A structured dictionary containing necessary credentials so the agent does not guess them (e.g. `{"username": "test_user", "password": "secure_password"}`).
*   **`user_journey_steps`**: A list of plain-text description lines detailing the actions the browser must take.

### 2. Crafting Optimal Journey Descriptions
The planner translates your instructions into structured actions (navigate, fill, click, select, wait). To guarantee 100% accurate translation:
*   **Be Explicit about Selectors**: Mention CSS identifiers, classes, or name attributes if you know them.
    *   *Suboptimal*: "Click login"
    *   *Optimal*: "Click the button with selector `button[type='submit']`" or "Click `a.login-btn`"
*   **Direct Credential Injection**: Tell the agent exactly where to put credentials.
    *   *Optimal*: "Fill the username field `input[name='user']` with credentials username"
*   **Define Wait Conditions**: Network latency varies. Explicitly mention when to wait for loads or elements.
    *   *Optimal*: "Wait for selector `div.dashboard` to be visible" or "Wait 2000 milliseconds for dashboard to load"

### Example Optimized Input Payload:
```json
{
  "target_url": "https://httpbin.org/forms/post",
  "credentials": {
    "username": "tester",
    "password": "super-secret-password"
  },
  "user_journey_steps": [
    "Navigate to target URL",
    "Fill input[name='custname'] with credentials username",
    "Fill input[type='email'] with email 'tester@example.com'",
    "Click input[value='onion']",
    "Wait 1000 milliseconds",
    "Click button",
    "Wait for load"
  ]
}
```
