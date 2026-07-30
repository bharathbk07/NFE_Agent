# Jira story worker

**Layman role:** Ticket runner — picks up a labeled Jira story from chat, runs the same performance pipeline, and posts results back on the ticket.

> Not a class under `src/agents/`. It is a **LangGraph node + REST worker** triggered by the intent router (`jira_perf`). Documented under [`docs/workers/`](overview.md).

| | |
|--|--|
| **Chat node** | [`src/nodes/jira_story.py`](../../src/nodes/jira_story.py) (`run_jira_story`) |
| **Worker / pipeline** | [`src/integrations/jira/`](../../src/integrations/jira/) (`worker.py`, `pipeline.py`, `client.py`, …) |
| **CLI debug** | [`src/integrations/jira_runner.py`](../../src/integrations/jira_runner.py) |
| **Intent** | `jira_perf` via [Intent router](intent-router.md) |
| **Prompt** | None for the worker (templates only). Intent uses `intent_classifier.txt` |
| **LLM?** | No for Jira REST / comments — **deterministic**. Pipeline may use other agents when analyse runs |

← [Workers overview](overview.md) · Full setup: [Jira integration](jira-integration.md)

---

## What it does

When you say things like **“work on SCRUM-1”** or **“work on jira story”** in Studio:

1. Resolve which issue (key in message, or list open `nfe-agent` issues).
2. Parse the story description for `target_url`, recording name, workload, thresholds.
3. Require a Watch-me recording on disk (or block with instructions).
4. Run analyse → k6 → smoke (reuse recording; same core pipeline as chat).
5. Post an ADF **Test Report** comment (pass/fail, stats, heal notes).
6. Update lifecycle labels (`nfe-queued` / `nfe-running` / `nfe-blocked` / `nfe-done`).

If smoke/SLA fails or the run aborts, the comment leads with **Why it failed / stopped**, the story stays **`nfe-blocked`** (not `nfe-done`), and Confluence is **not** published. Only a passing smoke gets `nfe-done` + Confluence.

---

## Why it exists

Performance work often starts from a **story**, not a blank chat. Wiring Jira → NFE → comment keeps engineers in one tool and avoids copy-pasting paths and metrics by hand.

Automation uses **REST**, not Atlassian MCP, so CI-style runs stay reliable and token-scoped.

---

## How it works

```text
Chat: "work on SCRUM-1"
    → Intent router (jira_perf)
    → run_jira_story
         → JiraClient (Basic auth, site REST)
         → parse story YAML/JSON
         → recording gate
         → run_perf_for_request → analyse_traffic (+ final workload smoke)
         → comment_results (markup → ADF)
         → labels / status transition
```

| Module | Job |
|--------|-----|
| `jira_story.py` | Chat UX: pick issue, clarity asks, force/re-run |
| `worker.py` | Lifecycle, gates, call pipeline, comment |
| `pipeline.py` | Load recording → analyse → overlay workload → smoke |
| `comments.py` | Deterministic Test Report text |
| `adf.py` | Convert markup → Atlassian Document Format |
| `story_parser.py` | Extract NFE config from description |
| `security.py` | Redact secrets in comments |

---

## Where it is used

- **Studio chat** (primary): `work on SCRUM-1`, `work on jira story`, `force` / `re-run`
- **CLI**: `python -m src.integrations.jira_runner --issue KEY`
- Graph edge: `route_intent` → `run_jira_story` → END

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Transport | Jira Cloud REST + HTTP Basic | Same pattern as ops bots; MCP stays interactive-only |
| Auth | Email + API token (`JIRA_*` env) | Classic/unscoped token preferred for site URL |
| Comments | ADF via REST v3 | Cloud does not render wiki/Markdown from API |
| Pipeline | Reuses analyse / k6 / heal | One engine for chat and Jira |

---

## Security techniques

| Control | Why |
|---------|-----|
| Label gate (`nfe-agent`) | Only opted-in issues are processed |
| Comment sanitization / redaction | No passwords or tokens in Jira |
| URL policy on `target_url` | Same SSRF controls as browser nav |
| Credentials via recording + story `credentials:` | Prefer Watch-me store; optional YAML `credentials:` — never put passwords only in free-text AC |
| Fail closed on auth errors | Bad token does not silently “succeed” |

---

## Performance techniques

| Technique | Why |
|-----------|-----|
| No LLM for comments / ADF | Instant, stable reports |
| Reuse Watch-me recording | Skip headed capture when disk file exists |
| Skip Confluence publish inside analyse; publish once after final smoke | Avoid double pages |
| JQL + status filters | Only eligible To Do / In Progress issues |

---

## Related

- [Intent router](intent-router.md) — routes `jira_perf`  
- [Confluence publisher worker](confluence-publisher-worker.md) — optional run page + link on the comment  
- [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) — what “pass/fail” means  
- [Security](../security/security.md)  
