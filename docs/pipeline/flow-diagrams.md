# NFE Agent — flow diagrams

Three views of how work moves through the system. Rendered as Mermaid (GitHub / most Markdown previews).

| Diagram | Purpose |
|---------|---------|
| [1. User flow](#1-user-flow) | What a person does in Studio chat |
| [2. End-to-end pipeline](#2-end-to-end-pipeline) | System stages from intent → publish |
| [3. Data transmission](#3-data-transmission) | What data moves between components |

Also: ASCII architecture in [`README.md`](../../README.md) · Agents: [`agents/overview.md`](../agents/overview.md) · Workers: [`workers/overview.md`](../workers/overview.md)

---

## 1. User flow

How an operator uses NFE from LangGraph Studio (or CLI).

```mermaid
flowchart TD
  Start([Open Studio / CLI]) --> Chat[Send chat message]

  Chat --> Intent{Intent router}

  Intent -->|help / small talk| Conv[Conversation reply]
  Intent -->|Q&A on prior analysis| QA[Analysis QA]
  Intent -->|watch me URL| WM[Watch-me: click through app]
  Intent -->|describe journey| Nav[Navigator: bot drives browser]
  Intent -->|follow-up / re-run journey| Nav
  Intent -->|list / analyse recording| Reuse[Load saved recording]
  Intent -->|work on SCRUM-1| Jira[Jira story worker]

  WM --> Overlay[Headed Chromium + TXN overlay]
  Overlay --> Done{Done?}
  Done -->|Cancel| End1([Stop])
  Done -->|Done| Replay[Headless Run 2 + data randomize]

  Nav --> Plan[LLM plans Playwright steps]
  Plan --> Auto[2× headless capture + randomize]

  Reuse --> HasRuns{2 runs on disk?}
  HasRuns -->|Yes| Analyse
  HasRuns -->|Only Run 1| Replay

  Replay --> Analyse
  Auto --> Analyse
  Jira --> Gate{Recording exists?}
  Gate -->|No| Block[Comment nfe-blocked + instructions]
  Gate -->|Yes| Analyse

  Analyse[analyse_traffic: IR → k6 → smoke/load → heal]
  Analyse --> Report[Chat summary + HTML report]
  Analyse --> Pub{Confluence / Jira configured?}
  Pub -->|Yes| Deliver[Jira Test Report comment<br/>Confluence run page]
  Pub -->|No| Disk[Artifacts on disk only]

  Conv --> End2([End turn])
  QA --> End2
  Block --> End2
  Report --> End2
  Deliver --> End2
  Disk --> End2
```

**Typical happy paths**

| You say | What happens |
|---------|----------------|
| `watch me https://… username=… password=…` | You click; agent records → analyses → k6 |
| Free-text journey (no “watch me”) | Navigator plans + runs browser (`performance_analysis`) |
| Follow-up / “run that again” | `follow_up_analysis` → same orchestrate → Navigator path |
| `analyse saved recording` | Skip browser; re-run analyse from disk |
| `work on SCRUM-1` | Parse story → reuse recording → load run → Jira/Confluence |

**Jira without Studio chat:** poll/process from CLI — `.venv/bin/python -m src.integrations.jira_runner --issue SCRUM-1` or `--poll-once` (see [Jira integration](../workers/jira-integration.md)).

---

## 2. End-to-end pipeline

System stages from capture through delivery (deterministic script path highlighted).

```mermaid
flowchart LR
  subgraph Inputs
    A1[Chat / BA context]
    A2[Jira story YAML + AC]
    A3[Watch-me / Navigator]
  end

  subgraph Capture
    B1[Run 1 network + steps]
    B2[Run 2 replay + randomize]
    B3[Optional Run 3]
  end

  subgraph Analyse
    C1[Traffic diff]
    C2[Parameters vs correlations]
    C3[TXN grouping]
    C4[Load-Test IR]
  end

  subgraph EmitValidate
    D1[k6 emit protocol / hybrid]
    D2[CLI k6 run + abort thresholds]
    D3[Heal IR ≤2]
    D4[HTML + summary + points]
  end

  subgraph Deliver
    E1[artifacts/ app-scoped]
    E2[Jira ADF comment]
    E3[Confluence page]
  end

  A1 --> A3
  A2 --> Capture
  A3 --> B1 --> B2 --> Analyse
  B3 -.-> Analyse
  C1 --> C2 --> C3 --> C4
  C4 --> D1 --> D2 --> D3 --> D4
  D4 --> E1
  D4 --> E2
  D4 --> E3
```

```mermaid
flowchart TD
  subgraph Graph["LangGraph src/graph.py"]
    RI[route_intent]
    OJ[orchestrate_journey]
    WM[watch_me_record]
    RP[replay_recorded_journey]
    PN[plan_navigator_steps]
    RA[run_automation]
    LR[load_saved_recording]
    JS[run_jira_story]
    AT[analyse_traffic]
  end

  RI --> OJ
  RI --> LR
  RI --> JS
  OJ --> WM --> RP --> AT
  OJ --> PN --> RA --> AT
  LR --> AT
  JS --> AT
  AT --> OUT[Artifacts + chat + workers]
```

**Design rule:** LLMs plan / classify / answer. IR → k6 emit, heal rules, Jira comments, and Confluence pages are **deterministic / REST** (no LLM-authored scripts).

### Also in the pipeline

Details that sit beside the Mermaid stages (not every edge is drawn above):

| Topic | What to know | Doc |
|-------|----------------|-----|
| **Hybrid k6** | Most TXNs are `k6/http`; SPA login may use `k6/browser` then cookie-sync back to protocol | [Load-Test IR → k6](load-test-ir-and-k6.md) |
| **Two self-heals** | (1) k6 IR heal after smoke 4xx (≤2); (2) Playwright selector heal during capture — different code paths | [Smoke + self-heal](smoke-and-self-heal.md) |
| **Abort thresholds** | Workload runs can stop early on catastrophic fail rate / p99 (`NFE_K6_ABORT_*`); default smoke keeps full failure set for heal | [Smoke + self-heal](smoke-and-self-heal.md#threshold-watcher-abortonfail) |
| **Jira CLI** | Chat `work on SCRUM-1` or `jira_runner --issue` / `--poll-once` | [Jira integration](../workers/jira-integration.md) |
| **LLM routing** | Optional Cursor SDK for planning; Gemini for extraction / self-heal / classify (see `.env.example`, `LLM_MODELS`) | Root [README](../../README.md) setup |
| **MCP** | All servers `enabled: false` by default; core path is Playwright+CDP + CLI `k6 run` | [Optional MCPs](../mcp/optional-mcps.md) |

---

## 3. Data transmission

What data crosses each boundary (credentials, network, IR, results).

```mermaid
sequenceDiagram
  autonumber
  actor User as Operator
  participant Studio as LangGraph Studio
  participant Agents as Agents / nodes
  participant Browser as Playwright + CDP
  participant App as App under test
  participant Disk as artifacts/
  participant K6 as k6 CLI
  participant Jira as Jira REST
  participant Conf as Confluence REST

  User->>Studio: Chat message (+ optional credentials)
  Studio->>Agents: AgentState update

  alt Watch-me / Navigator
    Agents->>Browser: Steps / headed or headless
    Browser->>App: HTTP (user + XHR)
    App-->>Browser: HTML / JSON / Set-Cookie
    Browser-->>Agents: network_requests, timeline, cookies
    Agents->>Disk: recordings/app/flow.json<br/>(credentials if NFE_STORE_CREDENTIALS)
  else Reuse / Jira
    Agents->>Disk: Load recording + credentials
    Jira-->>Agents: Issue fields / AC / YAML
  end

  Agents->>Agents: Diff → params/correlations → IR
  Note over Agents: Password/username → IR vars<br/>CSRF/requestId → correlations
  Agents->>Disk: k6/app/flow.js + _ir.json
  Agents->>K6: k6 run (env: report paths)
  K6->>App: Protocol (or hybrid browser) load
  App-->>K6: Responses
  K6-->>Agents: exit code, summary.json, k6-points.json
  Agents->>Disk: html-report.html
  Agents-->>Studio: Chat summary (secrets redacted in outward text)

  opt Jira path
    Agents->>Jira: ADF Test Report comment<br/>(passwords redacted)
  end

  opt Completed run + publish gate
    Agents->>Conf: Run page + attachments<br/>(HTML-parity body)
  end
```

### Data stores (app-scoped)

```mermaid
flowchart TB
  subgraph Artifacts["artifacts/"]
    R["recordings/&lt;app&gt;/&lt;flow&gt;.json<br/>steps · run_records · credentials"]
    K["k6/&lt;app&gt;/<br/>.js · _ir.json · html-report · summary · points"]
    Kn["knowledge/&lt;app&gt;/…"]
    Rag["rag/chroma/"]
  end

  Capture --> R
  R --> Analyse
  Analyse --> K
  Analyse --> Kn
  Analyse --> Rag
  K --> Smoke[k6 run]
  Smoke --> K
  K --> Publish[Jira / Confluence]
```

| Payload | Where it lives | Sent to LLM? | Sent to Jira/Confluence? |
|---------|----------------|--------------|---------------------------|
| Username / password | Recording + IR + k6 `vars` (when store on) | May appear in heal/script context | **No** — comment redaction |
| Session cookies | k6 cookie jar at runtime | No (redacted in artifacts network) | No |
| CSRF / requestId | IR correlations → script extracts | Advice only | Fail URLs may appear (no secrets) |
| Network HAR-like logs | `run_records` on disk | Truncated / classified | No raw dump |
| HTML report / summary | `artifacts/k6/<app>/` | No | Attached / linked when publish OK |

---

## Related docs

- [Load-Test IR → k6](load-test-ir-and-k6.md)
- [Smoke + self-heal](smoke-and-self-heal.md)
- [App artifacts & knowledge](app-artifacts-and-knowledge.md)
- [Jira integration](../workers/jira-integration.md)
- [Confluence publishing](../workers/confluence-publishing.md)
- [Security](../security/security.md)
