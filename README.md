# NFE Agent

Chat-driven performance-test generator. You describe (or click through) a web journey; the agent captures protocol-level HTTP traffic, correlates dynamic values across runs, and emits a deterministic **k6** smoke script—without using an LLM to write the script.

**Product surface:** LangGraph Studio chat (`langgraph dev --allow-blocking`), an optional headed Chromium window for Watch-me recording, and chat-driven **Jira** story pickup (`work on SCRUM-1`).

---

## What it does

| Stage | Outcome |
|--------|---------|
| Capture | Two independent browser runs with CDP-grade network logs |
| Analyse | Parameters vs correlations, TXN grouping, auth/session fixes |
| Emit | Load-Test IR → k6 JS (protocol and/or hybrid browser login) |
| Validate | `k6 run` smoke (1 VU × 2 iterations) + HTML report + deterministic heal |
| Deliver | Artifacts on disk; optional Jira ADF **Test Report** comment |

Outputs land under `artifacts/k6/` (script, IR, HTML report) and `artifacts/recordings/` (reusable Watch-me captures).

---

## Architecture

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                         LangGraph Studio (chat)                          │
│                              AgentState                                  │
└─────────────────────────────────┬────────────────────────────────────────┘
                                  │
                                  ▼
                         ┌────────────────┐
                         │  route_intent  │
                         └───────┬────────┘
     ┌───────────┬───────────────┼───────────────┬──────────────┐
     ▼           ▼               ▼               ▼              ▼
 conversation  analysis_qa   run_jira_story  orchestrate /   reuse
 (end)         (end)         (end)           Watch-me /      recording
                                             Navigator
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
        Watch-me path      Navigator path      Saved recording
        headed record      plan steps          load disk JSON
              │                   │                   │
              ▼                   ▼                   │
        headless replay      2× headless runs        │
        + data randomize     + data randomize        │
              │                   │                   │
              └─────────┬─────────┴───────────────────┘
                        ▼
              ┌─────────────────────┐
              │   analyse_traffic   │
              │  (diff → IR → k6)   │
              └──────────┬──────────┘
                         ▼
              artifacts/k6 + chat summary
              (+ Jira comment when via jira_perf)
```

`src/graph.py` is a thin assembler (edges + compile). Node logic lives under [`src/nodes/`](src/nodes/).

### Repository layout

```text
NFE_Agent/
├── src/
│   ├── graph.py                 # Thin assembler: StateGraph edges + compile
│   ├── main.py                  # CLI entry (imports compiled graph)
│   ├── exceptions.py            # Typed errors; hard vs soft failure helpers
│   ├── nodes/                   # route, capture, analyse, orchestrate, jira_story
│   ├── security/                # URL/step policy, secrets, path jail
│   ├── integrations/jira/       # REST client, ADF comments, labels, worker
│   ├── integrations/jira_runner.py  # CLI debug (--check-auth, --issue)
│   ├── agents/                  # Intent, orchestrator, navigator, analyst, …
│   ├── tools/playwright_tool.py # CDP capture, Watch-me, journey replay
│   └── utils/
│       ├── data_randomization.py  # Run1 harvest → Run2 page.route mutation
│       ├── load_test_ir.py        # Deterministic Load-Test IR
│       ├── k6_generator.py        # IR → k6 JS (no LLM)
│       ├── k6_healer.py           # Smoke-driven IR fixes (auth, IDs, CSRF)
│       ├── k6_runner.py           # CLI k6 smoke + points enrichment
│       ├── k6_report_builder.py   # html-report.html
│       ├── recording_store.py     # artifacts/recordings/
│       └── correlation_noise.py   # Drop browser-header / cache-buster noise
├── prompts/                     # Versioned LLM prompts (local + optional Hub)
├── config/                      # Settings, observability, MCP registry
├── docs/
│   ├── jira-integration.md      # Jira setup, scopes, chat triggers
│   ├── security.md              # Threat model + env knobs
│   └── optional-mcps.md         # Optional k6 / Playwright / Atlassian MCP
├── tests/                       # Security, Jira, exceptions, core unit tests
├── .github/workflows/           # security-audit (pytest + pip-audit)
├── artifacts/k6/                # Generated scripts, IR, reports
├── artifacts/recordings/        # Saved Watch-me captures
└── langgraph.json               # Studio entry: src/graph.py:graph
```

### Shared state (`AgentState`)

The chatbot keeps a typed LangGraph state across nodes, including:

- **Journey:** `target_url`, `credentials`, `user_journey_steps`, `sub_tasks`
- **Captures:** `run_records` (Run 1 / Run 2 / optional Run 3)
- **Analysis:** `parameterizable_candidates`, `correlations`, `dependencies`, `transactions`
- **Randomization:** `randomization_ledger`, `randomization_state`, `non_randomizable_endpoints`
- **Jira:** `jira_issue_key`, `jira_candidate_keys`, `jira_awaiting_clarity`
- **Delivery:** `performance_test_output`, `recording_file`, chat `messages`

---

## End-to-end flows

### 1. Intent routing (every chat turn)

```text
START → route_intent → …
```

| Intent | Next node | Purpose |
|--------|-----------|---------|
| `conversation` | `respond_conversation` | Small talk / help |
| `analysis_qa` | `answer_analysis_question` | Q&A on prior analysis in-thread |
| `watch_me` | `orchestrate_journey` → Watch-me | You click; agent records |
| `performance_analysis` / `follow_up_analysis` | `orchestrate_journey` → Navigator | Bot plans + clicks |
| `reuse_recording` | `load_saved_recording` | Re-analyse disk capture |
| `jira_perf` | `run_jira_story` | Process a labeled Jira issue via REST |

### 2. Watch-me (interactive record → replay → k6)

```text
orchestrate_journey
  → watch_me_record          # headed Chromium + overlay + CDP
  → replay_recorded_journey  # headless Run 2 + HTTP payload randomization
  → analyse_traffic          # diff, IR, k6, smoke, heal
  → END
```

1. Chat: `watch me https://example.com/` (+ optional credentials).
2. **Run 1:** Headed browser; overlay supports Start/End TXN, Pause, Done, Cancel.
3. Steps + CDP network saved to `artifacts/recordings/<host>.json`.
4. **Run 2:** Headless replay of the same steps. Payload randomization middleware rewrites unique fields (email, orderId, …) via `page.route` and mocks non-randomizable third-party payment hosts.
5. **Analyse:** Differential correlation, TXN grouping, IR build, k6 emit, smoke + heal.
6. Chat returns a playbook summary + paths to script / IR / HTML report.

**Display required** for Watch-me (local desktop). Use `langgraph dev --allow-blocking`.

### 3. Natural-language journey (bot drives the browser)

```text
orchestrate_journey
  → plan_navigator_steps   # LLM → structured Playwright steps
  → run_automation         # Run 1 + Run 2 headless (+ randomization on Run 2)
  → analyse_traffic
  → END
```

Selector failures can trigger LLM self-heal (accessibility snapshot + alternate selector). URL navigation and Playwright actions pass through [`src/security/`](src/security/) policy first.

### 4. Reuse a saved recording

```text
list recordings
analyse saved recording
analyse saved recording opensource-demo.orangehrmlive.com
```

- **2 runs on disk** → `analyse_traffic` immediately (no browser).
- **Only Run 1** → headless replay for Run 2, then analysis.

Override store with `NFE_RECORDINGS_DIR`.

### 5. Analysis Q&A

After a successful run in the same Studio thread, ask follow-ups (e.g. “which values are correlated?”). `answer_analysis_question` uses prior state; it does not re-capture. Mentions of TXN / k6 can rebuild script artifacts from existing captures.

### 6. Jira story (chat-driven)

```text
route_intent (jira_perf)
  → run_jira_story
       ├─ resolve issue (key in message, or list nfe-agent To Do / In Progress)
       ├─ parse description YAML/JSON (target_url, recording, workload, …)
       ├─ gate on artifacts/recordings/<name>.json
       ├─ run analyse pipeline (reuse recording → IR → k6 → smoke)
       └─ post ADF Test Report comment + lifecycle labels
  → END
```

```text
work on SCRUM-1
work on jira story
run jira
```

- Issues need label **`nfe-agent`**. Credentials stay in env (`NFE_USER` / `NFE_PASS`), not in the story body.
- Missing recording → `nfe-blocked` + instructions; after Watch-me, add **`nfe-recording-ready`** and retry (or say **force** / **re-run**).
- Full setup, API token scopes, and troubleshooting: [`docs/jira-integration.md`](docs/jira-integration.md).

---

## Analysis pipeline (`analyse_traffic`)

Strict order; script generation is **deterministic** (no LLM in IR → k6).

```text
Run1 + Run2 network logs
        │
        ├─ TrafficAnalystAgent          differential token candidates
        ├─ ParameterAgent               tester-supplied fills → vars
        ├─ reconcile_analysis           param vs correlation
        ├─ filter randomization ledger  drop deliberate test-data diffs
        ├─ CorrelationClassifierAgent   LLM advice; optional Run 3
        ├─ TransactionAgent             group HTTP into TXNs
        │
        ├─ build_load_test_ir()
        │     • vars / correlations / transactions
        │     • CSRF → ${csrf_token} on auth/validate
        │     • browser login when SPA session cannot be protocol-only
        │     • create-resource id → ${requestId} on /requests/{id}
        │     • randomization flags / non-randomizable mocks
        │
        ├─ generate_k6_script(ir)       protocol and/or k6/browser hybrid
        ├─ k6 smoke (CLI)               1 VU × 2 iterations
        ├─ heal_load_test_ir (≤2)       auth, CSRF, requestId, chrome GETs
        └─ html-report.html             TXN iters vs req fails, URL+status
```

### Correlation vs parameters vs randomization

| Kind | Source | Script handling |
|------|--------|-----------------|
| **Parameter** | User-fed (username, remarks, amount) | `vars.*` (optionally randomized per VU) |
| **Correlation** | Server-generated (session cookie, CSRF, claim `data.id`) | Extract from prior response → pass downstream |
| **Randomization** | Deliberate Run2 payload rewrite | Ledger filters these out of correlation |

Noise dropped early: browser fingerprint headers, cache-busters (`rnd`, `timestamp`, …), parameterish search query keys.

### Auth & 4xx prevention (script quality)

- Stale captured CSRF literals are always replaced with `${csrf_token}`.
- Silent login failure (HTTP 200 + login form still present) is detected; persistent 401s convert Login to **browser mode** with cookie sync into the http jar.
- Create POST `data.id` is correlated as `${requestId}` so downstream `/requests/8` paths do not 403/404.
- Smoke treats **4xx as script failure**; **5xx is allowed** as application fault (`http.expectedStatuses` 2xx–3xx + 5xx).

### Protocol vs Chromium (why hybrid exists)

k6 is primarily a **protocol** load tool (`k6/http`). It is **not** “a browser” the way Selenium is. NFE defaults to protocol VUs (cheap, scalable), same idea as JMeter/NeoLoad thread groups with extractors.

| Mode | Engine | Used for |
|------|--------|----------|
| **Protocol** | `k6/http` | Most API/XHR load; correlations via extract → `${var}` |
| **Browser** | `k6/browser` + Chromium | Narrow fallback—usually SPA **login**—when session/CSRF cannot be replayed from HTTP alone |

**Correlation first (like JMeter / NeoLoad):** CSRF in HTML/headers, JSON `data.id`, `Set-Cookie` sessions are extracted and passed downstream. Chromium is **not** a substitute for that.

**When expressions are not enough:** the value never appears on the wire in a usable form (JS-only tokens), or login returns a silent **200** with the login page still shown so extractors have nothing valid to bind—then APIs cascade **401**. Hybrid browser login establishes a real session, syncs cookies into the http jar, and **the rest of the journey stays protocol**.

**High volume (e.g. 1k VUs):** do **not** run 1k Chromium instances—that burns RAM/CPU. Use hybrid for smoke / low concurrency until auth is green, then prefer **protocol-only** login (or a small auth setup + shared tokens) and scale with `k6/http`. Chromium is a correctness bridge; protocol HTTP is the scale path.

### HTML report metrics

- **Count** = TXN iterations  
- **Failed iters** ≤ count (iteration-level)  
- **Req fails** = request-level failures inside the TXN  
- Failed URL list includes HTTP status (0 = network / blocked)

---

## Artifacts

| Path | Contents |
|------|----------|
| `artifacts/k6/<host>.js` | Generated k6 script (overwritten on heal) |
| `artifacts/k6/<host>_ir.json` | Load-Test IR |
| `artifacts/k6/html-report.html` | Last smoke report |
| `artifacts/k6/k6-points.json` | k6 `--out json` samples |
| `artifacts/k6/summary.json` | k6 handleSummary metrics |
| `artifacts/recordings/<host>.json` | Watch-me steps + run records |

Install k6 for smoke: [Install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/). Smoke uses **CLI** `k6 run` (needed for JSON points + HTML). Grafana k6 MCP is optional and off by default (`NFE_K6_MCP=mcp`); see [`docs/optional-mcps.md`](docs/optional-mcps.md).

---

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install
cp .env.example .env   # set GEMINI_API_KEY (and optional keys)
```

Minimal `.env`:

```ini
GEMINI_API_KEY=your-key
GEMINI_MODEL=gemini-2.5-flash
```

Optional: multi-model routing (`LLM_MODELS`), Cursor SDK (`CURSOR_API_KEY`), LangSmith, Dynatrace OTLP, Jira, security policy—see [`.env.example`](.env.example).

App MCP servers (not Cursor IDE): [`config/mcp_servers.json`](config/mcp_servers.json). All stay **disabled** by default; npm packages are version-pinned (no `@latest`).

### Run the chatbot UI

```bash
pip install langgraph-cli
langgraph dev --allow-blocking
```

Open the printed Studio URL (typically `http://localhost:2024`).  
`--allow-blocking` is required for Playwright / Watch-me under the LangGraph runtime.

Bind Studio to **localhost** only; do not expose it on a shared network without auth. See [Security](#security).

Graph entry: [`langgraph.json`](langgraph.json) → `src/graph.py:graph` (nodes live under [`src/nodes/`](src/nodes/)).

### Run the CLI

```bash
python -m src.main -m 'watch me https://example.com username=u password=p'
python -m src.main config.json -o result.json
```

`src/main.py` imports the same compiled graph as Studio and invokes it with a chat message or JSON config (`target_url` / `credentials` / `user_journey_steps` or `message`).

### Security

Controls live under [`src/security/`](src/security/). Full threat model: [`docs/security.md`](docs/security.md).

| Control | Default | Role |
|---------|---------|------|
| URL policy | deny private / metadata hosts | Blocks SSRF-style `page.goto` targets |
| Step policy | allowlisted Playwright actions | Rejects unsafe browser actions |
| Secrets | placeholders to LLMs | Credentials not sent as plaintext to planners |
| Artifact redaction | on | Masks auth headers / cookies / password fields |
| Path jail | on | Recordings + k6 filenames stay under `artifacts/` |
| Credential store | off | Passwords not written into recordings/IR |

Typed exceptions in [`src/exceptions.py`](src/exceptions.py):

- **Fail closed:** `NFESecurityError`, `NFEConfigError`, `NFEAuthError`
- **Soft-fail** (chat / `error_log`): pipeline / validation / most integration errors  
  User-facing text and Jira comments are redacted (no passwords, tokens, or stack traces).

k6 scripts read credentials from the environment at runtime:

```bash
NFE_USER=Admin NFE_PASS=secret k6 run artifacts/k6/<host>.js
```

CI: [`.github/workflows/security-audit.yml`](.github/workflows/security-audit.yml) runs unit tests (security, Jira, exceptions) and informational `pip-audit`.

### Jira

Stories labeled **`nfe-agent`** are processed from **Studio chat** (primary) or CLI debug. Lifecycle labels: `nfe-queued` / `nfe-running` / `nfe-blocked` / `nfe-recording-ready` / `nfe-done`. Atlassian MCP is optional and unused by the worker (REST only).

Setup, **API token scopes**, and troubleshooting: [`docs/jira-integration.md`](docs/jira-integration.md).

```text
work on SCRUM-1
work on jira story
```

```bash
.venv/bin/python -m src.integrations.jira_runner --check-auth
.venv/bin/python -m src.integrations.jira_runner --issue SCRUM-1
```

Minimal Jira `.env` (prefer a classic / unscoped API token for site URL + Basic auth):

```ini
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=bot@example.com
JIRA_API_TOKEN=your_api_token_here
NFE_JIRA_LABEL=nfe-agent
NFE_USER=Admin
NFE_PASS=secret
```

---

## Using the chat

### Watch-me

```text
watch me https://opensource-demo.orangehrmlive.com/web/index.php/auth/login
username=Admin password=admin123
```

In the browser overlay: **Start TXN** / **End TXN**, **Done** (or **Cancel**).

### Navigator (bot clicks)

Prefer explicit selectors and credential injection:

```json
{
  "target_url": "https://httpbin.org/forms/post",
  "credentials": { "username": "tester", "password": "secret" },
  "user_journey_steps": [
    "Navigate to target URL",
    "Fill input[name='custname'] with credentials username",
    "Click button",
    "Wait for load"
  ]
}
```

Or free-text journey steps in chat (omit “watch me”).

### Jira

```text
work on SCRUM-1
work on jira story
work on SCRUM-1 force
```

### Reuse / Q&A

```text
list recordings
analyse saved recording
Which values are correlated for login?
```

### LangSmith vs recordings

| Need | Use |
|------|-----|
| Re-run analysis on the same clicks/network | `artifacts/recordings/*.json` |
| Debug LLM/tool traces | LangSmith (`LANGCHAIN_TRACING_V2` + API key) |

LangSmith does **not** store Watch-me captures. New Studio threads need the disk recording to reuse a prior capture.

---

## Prompts

LLM prompts live under [`prompts/`](prompts/) and are loaded via `src/utils/prompt_loader.py`.

1. **LangSmith Hub** (optional): when configured, named prompts can be pulled at runtime.
2. **Local files**: Git-tracked fallback for offline / default use (`USE_LANGSMITH_PROMPTS` defaults off to avoid blocking the event loop).

Script generation and healing do **not** use these prompts—only planning, classification, and self-heal do.

---

## Tests

```bash
pytest tests/ -q --ignore=tests/test_run.py
```

Covers security policy, Jira chat/integration helpers, typed exceptions, and core pipeline unit tests.

---

## Design principles

1. **Protocol-first** — Capture and mutate at HTTP/CDP level; UI locators are for replay, not for load-test data.
2. **Deterministic compiler** — Same IR always emits the same k6; LLMs advise, they do not author scripts.
3. **Two-run differential** — Dynamic tokens are proven by Run1 vs Run2, then origin-traced to prior responses.
4. **Heal script bugs, not the app** — Fix CSRF, session, and create-IDs that cause 4xx; allow application 5xx.
5. **Stable artifacts** — One script/IR per host; heals overwrite in place so deliverables stay predictable.
6. **Fail closed on security** — URL/step/fs-jail violations never bypass; secrets stay out of LLM prompts, logs, and Jira comments.
7. **Thin graph, fat nodes** — `graph.py` wires edges only; capture, analyse, routing, and Jira live in `src/nodes/`.
