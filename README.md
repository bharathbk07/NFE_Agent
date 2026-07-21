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

# LangSmith Configuration (Optional — traces + prompt hub; NOT a recording store)
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
langgraph dev --allow-blocking
```
Once started, the CLI will output a local URL (typically `http://localhost:2024` or similar). Open this URL in your web browser or load the directory in the desktop version of **LangGraph Studio** to interact with the graph.

`--allow-blocking` is required for Playwright (and Watch-me headed capture) under the LangGraph runtime.

---

## Watch-me mode (interactive recording)

Stay in one product: chat plus a browser window the agent opens. No DevTools, no HAR export.

1. In Studio chat, send a URL (credentials optional) and say **watch me** / **record while I click**.
2. The agent opens a **headed** Chromium window at that URL.
3. Click through your journey. Use the overlay like commercial recorders:
   - **Start TXN** — type a name (e.g. `Login`, `Assign_Claim`) then confirm before that phase
   - **End TXN** — close the current transaction (optional; starting another TXN auto-ends the previous)
   - **Pause** / **Resume**, **Done**, or **Cancel**; drag the handle to move the panel
4. Click **Done** when finished (or **Cancel** to abort without analysis).
5. The agent saves your steps + network as Run 1, **auto-replays** headless as Run 2, then runs analysis and emits a **k6 smoke script** (`1 VU` × `2 iterations`) as a single stable file per host (`artifacts/k6/<host>.js`, overwritten on heal — not a new file each attempt).
6. The capture is also written to **`artifacts/recordings/<host>.json`** so you can re-analyse without re-recording (see below).
7. If `k6` is on your `PATH`, the agent **runs the smoke** and applies deterministic self-heal (drop chrome GETs, relax optional checks, retarget extracts) up to twice before delivering the file. Each run writes **`artifacts/k6/html-report.html`** with general details, observations, full TXN table (min/max/avg/percentiles), full request table (method/URL/failures), failed requests with **URL + status**, and SLA thresholds. Generated scripts assert each response (status, body, JSON when applicable).

### Reuse a saved recording (no re-record)

After one Watch-me session, chat:

```text
list recordings
analyse saved recording
analyse saved recording opensource-demo.orangehrmlive.com
```

- **2 runs already on disk** → analysis + k6 immediately (no browser).
- **Only Run 1** → headless replay for Run 2, then analysis.
- Override store path with `NFE_RECORDINGS_DIR` if needed.

### LangSmith vs saved recordings

| Need | Use |
|------|-----|
| Re-run analysis on the same clicks/network | Disk store: `artifacts/recordings/*.json` + chat above |
| Debug LLM/tool traces, prompt versions | LangSmith (`LANGCHAIN_TRACING_V2` + API key) |

LangSmith traces Studio **runs** (inputs/outputs per node). It does **not** replace Watch-me capture storage. Thread state in Studio can keep `run_records` in one thread, but a new thread or restart needs the disk recording.

Correlation focuses on cookies, body/query tokens, and auth/CSRF headers — not generic request headers (Accept, User-Agent, sec-fetch-*, etc.).

Install k6 for smoke validation: [Install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/). Smoke/heal uses **CLI** `k6 run` (writes `html-report.html`). Grafana k6 MCP is optional and off by default (stdio can crash with `BrokenResourceError`); see [`docs/optional-mcps.md`](docs/optional-mcps.md).

**Requirements:** a local display (macOS/Linux desktop). Remote or headless-only Studio hosts without a display are unsupported for Watch-me — use local `langgraph dev --allow-blocking`.

**Example prompt:**
```text
watch me https://www.saucedemo.com/
username=standard_user password=secret_sauce
```

Natural-language journey analysis (bot plans and clicks for you) still works as before — omit “watch me” and include journey steps.

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
