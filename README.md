# NFE Agent

**Agentic AI assistant for Performance Testing & Engineering.**

The long-term product outcome: an agent that reads **BA / requirements documents** for an application, builds an understanding of the whole system and critical user flows, **creates performance scripts**, **runs** them under load, **analyzes** the results, and delivers **RCA + fix suggestions**—not just a generated file on disk.

**Today (MVP):** chat-driven path from a real browser journey (or a Jira story) → stable **k6** script → smoke/load run → HTML report → optional **Jira** comment + **Confluence** publish. Script generation is **deterministic** (Load-Test IR → k6); LLMs plan, classify, and advise—they do not invent the load script.

**Product surface:** LangGraph Studio chat (`langgraph dev --allow-blocking`), optional headed Chromium for Watch-me recording, chat-driven **Jira** pickup (`work on SCRUM-1`), and **Confluence** run pages with HTML-parity findings.

---

## Vision → what ships today

| Capability | Vision | MVP status |
|------------|--------|------------|
| Understand the application | Read BA docs, stories, acceptance criteria; map flows & risk | Jira story YAML/AC + Watch-me journey; BA-doc ingestion expanding |
| Create performance scripts | Parameterization + correlation for real user flows | Watch-me / Navigator → IR → deterministic k6 (protocol + hybrid login) |
| Run tests | Smoke + load workloads from story | CLI `k6 run`; story workload (VUs/iterations); catastrophic abort (≥60% fail rate) |
| Analyze results | SLA, TXN/request tables, error patterns | HTML report + summary metrics; Jira “Why it failed” |
| RCA & fix suggestions | Root cause + what to fix (script vs app) | Heal notes for script/correlation bugs; 4xx vs 5xx policy; Confluence/Jira findings |
| Delivery | Durable evidence for the team | Artifacts on disk; Jira ADF report; Confluence page + attachments |

```text
BA / Jira story / chat intent
        │
        ▼
Understand flows (recording + story context)
        │
        ▼
Build Load-Test IR → emit k6 (no LLM authoring)
        │
        ▼
Run smoke / load → HTML + metrics
        │
        ▼
Analyze → RCA signals (auth, correlation, SLA, abort)
        │
        ▼
Publish findings (Jira comment + Confluence run page)
```

---

## What it does

| Stage | Outcome |
|--------|---------|
| Capture | Two independent browser runs with CDP-grade network logs (app-scoped recordings) |
| Analyse | Parameters vs correlations, TXN grouping, auth/session + create-ID fixes |
| Emit | Load-Test IR → k6 JS (protocol and/or hybrid browser login) |
| Validate | `k6 run` smoke/load + HTML report + deterministic heal; abort on catastrophic fail rate |
| Deliver | Artifacts under `artifacts/{recordings,k6,knowledge}/<app>/…`; Jira Test Report; Confluence publish |

Outputs are **per application domain** (e.g. `opensource-demo.orangehrmlive.com`), not a single flat host file.

**Flow diagrams (Mermaid):** [User flow · End-to-end · Data transmission](docs/pipeline/flow-diagrams.md) · [Load-Test IR → k6](docs/pipeline/load-test-ir-and-k6.md)

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
              artifacts + chat summary
              (+ Jira comment / Confluence when configured)
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
│   ├── integrations/confluence/ # Publish completed runs (HTML-parity body)
│   ├── integrations/jira_runner.py  # CLI debug (--check-auth, --issue)
│   ├── agents/                  # Intent, orchestrator, navigator, analyst, …
│   ├── tools/playwright_tool.py # CDP capture, Watch-me, journey replay
│   └── utils/
│       ├── app_registry.py / workspace.py / knowledge_store.py / rag_store.py
│       ├── data_randomization.py  # Run1 harvest → Run2 page.route mutation
│       ├── load_test_ir.py        # Deterministic Load-Test IR
│       ├── k6_generator.py        # IR → k6 JS (no LLM); catastrophic abort thresholds
│       ├── k6_healer.py           # Smoke-driven IR fixes (auth, IDs, CSRF)
│       ├── k6_runner.py           # CLI k6 + points enrichment
│       ├── k6_report_builder.py   # html-report.html
│       ├── recording_store.py     # artifacts/recordings/<app>/<flow>.json
│       └── correlation_noise.py   # Drop browser-header / cache-buster noise
├── prompts/                     # Versioned LLM prompts (local + optional Hub)
├── config/                      # Settings, observability, MCP registry
├── docs/                        # See docs/README.md for full index
├── tests/                       # Security, Jira, Confluence, exceptions, core unit tests
├── .github/workflows/           # security-audit (pytest + pip-audit)
├── artifacts/
│   ├── k6/<app>/                # Scripts, IR, HTML, summary, points
│   ├── recordings/<app>/        # Watch-me captures
│   ├── knowledge/<app>/         # Flow markdown knowledge
│   └── rag/chroma/              # Local ChromaDB
└── langgraph.json               # Studio entry: src/graph.py:graph
```

**Documentation:** full index at [`docs/README.md`](docs/README.md). Agents: [`docs/agents/overview.md`](docs/agents/overview.md). Workers: [`docs/workers/overview.md`](docs/workers/overview.md).

### Shared state (`AgentState`)

The chatbot keeps a typed LangGraph state across nodes, including:

- **Journey:** `target_url`, `credentials`, `user_journey_steps`, `sub_tasks`
- **App scope:** `app` (domain), `flow` (recording / Watch-me name)
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

How each agent works: [`docs/agents/overview.md`](docs/agents/overview.md).  
Jira / Confluence workers: [`docs/workers/overview.md`](docs/workers/overview.md).

### 2. Watch-me (interactive record → replay → k6)

```text
orchestrate_journey
  → watch_me_record          # headed Chromium + overlay + CDP
  → replay_recorded_journey  # headless Run 2 + HTTP payload randomization
  → analyse_traffic          # diff, IR, k6, smoke, heal, Confluence
  → END
```

1. Chat: `watch me https://example.com/` (+ optional credentials).
2. **Run 1:** Headed browser; overlay supports Start/End TXN, Pause, Done, Cancel.
3. Steps + CDP network saved to `artifacts/recordings/<app>/<flow>.json` (credentials stored when `NFE_STORE_CREDENTIALS=true`).
4. **Run 2:** Headless replay. Payload randomization rewrites unique fields via `page.route`.
5. **Analyse:** Differential correlation, TXN grouping, IR, k6 emit, smoke + heal.
6. Chat returns playbook summary + paths; completed runs can publish to Confluence.

**Display required** for Watch-me. Use `langgraph dev --allow-blocking`.

### 3. Natural-language journey (bot drives the browser)

```text
orchestrate_journey
  → plan_navigator_steps   # LLM → structured Playwright steps
  → run_automation         # Run 1 + Run 2 headless (+ randomization on Run 2)
  → analyse_traffic
  → END
```

Selector failures can trigger LLM self-heal. URL navigation and Playwright actions pass through [`src/security/`](src/security/) first.

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

After a successful run in the same Studio thread, ask follow-ups (e.g. “which values are correlated?”). Mentions of TXN / k6 can rebuild script artifacts from existing captures.

### 6. Jira story (chat-driven)

```text
route_intent (jira_perf)
  → run_jira_story
       ├─ resolve issue (key in message, or list nfe-agent To Do / In Progress)
       ├─ parse description YAML/JSON (target_url, recording, workload, credentials, …)
       ├─ gate on artifacts/recordings/<app>/<flow>.json
       ├─ analyse → emit k6 with story workload → run → Confluence
       └─ post ADF Test Report comment + lifecycle labels
  → END
```

```text
work on SCRUM-1
work on jira story
```

- Issues need label **`nfe-agent`**.
- **Credentials** come from the Watch-Me recording and/or story `credentials:` block (per app)—not a single global `NFE_USER`/`NFE_PASS`.
- Missing recording → `nfe-blocked` + instructions; after Watch-me, add **`nfe-recording-ready`** and retry (or say **force** / **re-run**).
- Full setup: [`docs/workers/jira-integration.md`](docs/workers/jira-integration.md). Worker: [`docs/workers/jira-story-worker.md`](docs/workers/jira-story-worker.md).

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
        │     • vars / correlations / transactions (+ app credentials)
        │     • CSRF → ${csrf_token} on auth/validate
        │     • browser login when SPA session cannot be protocol-only
        │     • create-resource id → ${requestId} on /requests/{id}
        │     • randomization flags / non-randomizable mocks
        │
        ├─ generate_k6_script(ir)       protocol and/or k6/browser hybrid
        │     • SLA thresholds (end-of-test fail)
        │     • catastrophic abort (e.g. fail rate ≥ 60%, extreme p99)
        ├─ k6 smoke / story workload
        ├─ heal_load_test_ir (≤2)       auth, CSRF, requestId, chrome GETs
        ├─ html-report.html             TXN / request / failed tables
        └─ Confluence (completed runs; skip dominant script 4xx)
```

### Correlation vs parameters vs randomization

| Kind | Source | Script handling |
|------|--------|-----------------|
| **Parameter** | User-fed (username, password, remarks, amount) | `vars.*` embedded from recording (per app) |
| **Correlation** | Server-generated (session cookie, CSRF, claim `data.id`) | Extract from prior response → pass downstream |
| **Randomization** | Deliberate Run2 payload rewrite | Ledger filters these out of correlation |

### Auth & 4xx prevention (script quality)

- Stale captured CSRF literals are always replaced with `${csrf_token}`.
- Silent login failure (HTTP 200 + login form still present) is detected; persistent 401s convert Login to **browser mode** with cookie sync into the http jar.
- Create POST `data.id` is correlated as `${requestId}` so downstream paths do not 404 with empty IDs.
- Smoke treats **4xx as script failure**; **5xx is allowed** as application fault.
- Load runs abort early when HTTP fail rate exceeds **`NFE_K6_ABORT_FAIL_RATE`** (default **0.60** / 60%), with optional p99 / checks collapse guards.

Full walkthrough: [`docs/pipeline/smoke-and-self-heal.md`](docs/pipeline/smoke-and-self-heal.md). IR → k6: [`docs/pipeline/load-test-ir-and-k6.md`](docs/pipeline/load-test-ir-and-k6.md).

### Protocol vs Chromium (why hybrid exists)

| Mode | Engine | Used for |
|------|--------|----------|
| **Protocol** | `k6/http` | Most API/XHR load; correlations via extract → `${var}` |
| **Browser** | `k6/browser` + Chromium | Narrow fallback—usually SPA **login**—then back to HTTP |

**High volume:** do **not** run thousands of Chromium instances. Hybrid is a correctness bridge; protocol HTTP is the scale path.

### HTML report & Confluence parity

- KPI strip, observations, full TXN / request / failed-request tables, SLA PASS/FAIL
- Confluence run pages mirror the HTML report (coloured status macros + tables) when publish is allowed
- Dominant **script 4xx** failures skip Confluence (`script_4xx_failures`) so broken scripts do not overwrite good pages

---

## Artifacts

| Path | Contents |
|------|----------|
| `artifacts/k6/<app>/<flow>.js` | Generated k6 script |
| `artifacts/k6/<app>/<flow>_ir.json` | Load-Test IR |
| `artifacts/k6/<app>/html-report.html` | Last smoke/load report |
| `artifacts/k6/<app>/k6-points.json` | k6 `--out json` samples |
| `artifacts/k6/<app>/summary.json` | k6 handleSummary metrics |
| `artifacts/recordings/<app>/<flow>.json` | Watch-me steps + run records (+ credentials when store on) |
| `artifacts/knowledge/<app>/…` | Flow markdown knowledge |
| `artifacts/rag/chroma/` | Local ChromaDB |

App / flow layout: [`docs/pipeline/app-artifacts-and-knowledge.md`](docs/pipeline/app-artifacts-and-knowledge.md).

Install k6: [Install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/). Smoke uses CLI `k6 run` by default (MCP k6 stays disabled — see [Optional MCP servers](#optional-mcp-servers-enabled-false-by-default)).

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
NFE_STORE_CREDENTIALS=true
```

Optional: multi-model routing (`LLM_MODELS`), Cursor SDK, LangSmith, Dynatrace OTLP, Jira, Confluence, abort thresholds—see [`.env.example`](.env.example).

### Run the chatbot UI

```bash
pip install langgraph-cli
langgraph dev --allow-blocking
```

Open the Studio URL (typically `http://localhost:2024`).  
`--allow-blocking` is required for Playwright / Watch-me.

Bind Studio to **localhost** only. See [Security](#security).

### Run the CLI

```bash
python -m src.main -m 'watch me https://example.com username=u password=p'
python -m src.main config.json -o result.json
```

### Security

Controls: [`src/security/`](src/security/). Full model: [`docs/security/security.md`](docs/security/security.md).

| Control | Default | Role |
|---------|---------|------|
| URL policy | deny private / metadata hosts | Blocks SSRF-style `page.goto` targets |
| Step policy | allowlisted Playwright actions | Rejects unsafe browser actions |
| Credential store | **on** | Per-app username/password in recording → IR → k6 (scales across apps) |
| Artifact redaction | on | Masks auth headers / cookies in network captures; password fills kept when store on |
| Comment redaction | on | Jira/Confluence still redact password-like text |
| Path jail | on | Recordings + k6 stay under `artifacts/` |

Typed exceptions: [`src/exceptions.py`](src/exceptions.py) — fail closed on security/config/auth; soft-fail elsewhere.

CI: [`.github/workflows/security-audit.yml`](.github/workflows/security-audit.yml).

### Jira

Stories labeled **`nfe-agent`** from Studio chat (or CLI debug). Lifecycle labels: `nfe-queued` / `nfe-running` / `nfe-blocked` / `nfe-recording-ready` / `nfe-done`.

```text
work on SCRUM-1
work on jira story
```

```bash
.venv/bin/python -m src.integrations.jira_runner --check-auth
.venv/bin/python -m src.integrations.jira_runner --issue SCRUM-1
```

```ini
JIRA_BASE_URL=https://your-site.atlassian.net
JIRA_EMAIL=bot@example.com
JIRA_API_TOKEN=your_api_token_here
NFE_JIRA_LABEL=nfe-agent
```

Story YAML may include workload + per-app credentials:

```yaml
target_url: https://opensource-demo.orangehrmlive.com/...
recording: default
workload:
  vus: 10
  iterations: 20
  maxDuration: 5m
credentials:
  username: Admin
  password: admin123
```

### Confluence

Completed runs publish under **Performance Testing and Engineering** with k6 + HTML + IR attachments and an HTML-parity storage body. Mid-run infra aborts and dominant script-4xx failures are **not** published.

```ini
CONFLUENCE_SPACE_KEY=ENG
NFE_CONFLUENCE_PUBLISH=true
# Empty BASE/EMAIL/TOKEN → fall back to JIRA_*
```

Setup: [`docs/workers/confluence-publishing.md`](docs/workers/confluence-publishing.md).

### Optional MCP servers (`enabled: false` by default)

Project MCP registry: [`config/mcp_servers.json`](config/mcp_servers.json) (loaded by the LangGraph app — **not** Cursor IDE `.cursor/mcp.json`).

Every server ships with **`"enabled": false`**. The core pipeline does **not** need MCP:

| Server | Default | Why off | Core path instead |
|--------|---------|---------|-------------------|
| `k6` | `false` | Smoke/heal need CLI output for HTML + points | CLI `k6 run` |
| `playwright` | `false` | Extra process; not used for capture | In-process Playwright + CDP |
| `chrome-devtools` | `false` | Optional live traces only | CDP network in capture |
| `atlassian` | `false` | Avoid Rovo MCP auth on every start | Jira/Confluence **REST** workers |

Packages are **version-pinned** (no `@latest`) to avoid surprise upgrades.

**Enable only when you need that MCP in-bot:**

1. Set `"enabled": true` on the server in `config/mcp_servers.json`
2. For k6 MCP tools: also set `NFE_K6_MCP=mcp` in `.env` (default remains `cli`)
3. Restart Studio / `langgraph`

Full details: [`docs/mcp/optional-mcps.md`](docs/mcp/optional-mcps.md).

---

## Using the chat

### Watch-me

```text
watch me https://opensource-demo.orangehrmlive.com/web/index.php/auth/login
username=Admin password=admin123
```

### Navigator (bot clicks)

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

### Jira / reuse / Q&A

```text
work on SCRUM-1
list recordings
analyse saved recording
Which values are correlated for login?
```

---

## Prompts

LLM prompts live under [`prompts/`](prompts/). Script generation and healing do **not** use these—only planning, classification, and browser self-heal do.

---

## Documentation

| Topic | Doc |
|-------|-----|
| Index | [`docs/README.md`](docs/README.md) |
| Agents overview | [`docs/agents/overview.md`](docs/agents/overview.md) |
| Workers overview | [`docs/workers/overview.md`](docs/workers/overview.md) |
| Jira setup | [`docs/workers/jira-integration.md`](docs/workers/jira-integration.md) |
| Confluence setup | [`docs/workers/confluence-publishing.md`](docs/workers/confluence-publishing.md) |
| Smoke + self-heal | [`docs/pipeline/smoke-and-self-heal.md`](docs/pipeline/smoke-and-self-heal.md) |
| Flow diagrams (user / E2E / data) | [`docs/pipeline/flow-diagrams.md`](docs/pipeline/flow-diagrams.md) |
| Load-Test IR → k6 | [`docs/pipeline/load-test-ir-and-k6.md`](docs/pipeline/load-test-ir-and-k6.md) |
| App artifacts & knowledge | [`docs/pipeline/app-artifacts-and-knowledge.md`](docs/pipeline/app-artifacts-and-knowledge.md) |
| Security | [`docs/security/security.md`](docs/security/security.md) |
| Optional MCPs (`enabled: false` defaults) | [`docs/mcp/optional-mcps.md`](docs/mcp/optional-mcps.md) |

---

## Tests

```bash
pytest tests/ -q --ignore=tests/test_run.py
```

---

## Design principles

1. **Agentic PTE outcome** — From requirements/journey understanding → script → run → analysis → RCA/fix evidence for the team.
2. **Protocol-first** — Capture at HTTP/CDP; UI locators are for replay, not load-test data.
3. **Deterministic compiler** — Same IR always emits the same k6; LLMs advise, they do not author scripts.
4. **Two-run differential** — Dynamic tokens proven by Run1 vs Run2.
5. **Heal script bugs, not the app** — Fix CSRF/session/IDs that cause 4xx; allow application 5xx.
6. **Per-app credentials & artifacts** — Scale across applications without a global env credential pair.
7. **Fail closed on security** — URL/step/fs-jail violations never bypass; outward comments still redact secrets.
8. **Thin graph, fat nodes** — `graph.py` wires edges only; logic lives in `src/nodes/` and workers.
