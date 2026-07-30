# Confluence publisher worker

**Layman role:** Report librarian — after a **completed** k6 run, files the results under a Confluence space (page + HTML/k6 attachments).

> Not a class under `src/agents/`. It is a **deterministic publisher** in [`src/integrations/confluence/`](../../src/integrations/confluence/). Documented under [`docs/workers/`](overview.md).

| | |
|--|--|
| **Code** | [`src/integrations/confluence/`](../../src/integrations/confluence/) (`publisher.py`, `client.py`, `report.py`, `pages.py`) |
| **Called from** | [`analyse_traffic`](../../src/nodes/analyse.py) (Studio/CLI); [`jira/pipeline.py`](../../src/integrations/jira/pipeline.py) after final smoke |
| **Prompt** | None |
| **LLM?** | **No** — templates + REST only |

← [Workers overview](overview.md) · Full setup: [Confluence publishing](confluence-publishing.md)

---

## What it does

When a smoke/load run **fully finishes** (SLA may pass or fail):

1. Find-or-create parent page **“Performance Testing and Engineering”** in `CONFLUENCE_SPACE_KEY`.
2. Find-or-create a **flow** page named after the Watch-me / recording (e.g. `Create Claim`).
3. Create a dated child **`Run YYYY-MM-DD HH:MM`** with an **HTML-parity** report (KPIs, observations, TXN/request/failed tables, coloured status macros, SLA).
4. Attach **k6 script**, **HTML report**, and optional **IR JSON**.
5. Update the flow page with a “Latest run” summary + link.

**Does not publish** if the run stopped mid-way (timeout, abort, k6 missing/skipped), or if failures are **dominated by HTTP 4xx** (script/correlation bugs — skip reason `script_4xx_failures`).

---

## Why it exists

Artifacts on disk (`artifacts/k6/<app>/…`) are easy to lose in chat noise. Confluence gives the team a durable, searchable home per user flow — without asking an LLM to write wiki pages.

---

## How it works

```text
analyse / Jira pipeline finishes smoke
    → should_publish_to_confluence(smoke, summary.json)?
         ├─ no  → skip (still may comment on Jira with why stopped)
         └─ yes → publish_run_results
                    → ConfluenceClient (Basic auth)
                    → storage-format XHTML body
                    → upload attachments
                    → return run_url (chat + Jira comment)
```

| Module | Job |
|--------|-----|
| `publisher.py` | Gate + orchestration (`try_publish_run_results` soft-fails) |
| `client.py` | REST: search/create/update page, attachments |
| `pages.py` | Find-or-create helpers |
| `report.py` | Deterministic storage HTML (status, metrics, SLA) |
| `security.py` | Title sanitize + body redaction |

### Publish rules (locked)

| Outcome | Publish? |
|---------|----------|
| Smoke **passed** | Yes |
| Smoke/SLA **failed** | **No** |
| Mid-run abort / timeout / skipped | **No** |

---

## Where it is used

- After Studio/CLI `analyse_traffic` (unless `skip_confluence_publish`).
- After Jira `run_perf_for_request` final smoke (analyse skips Confluence there to avoid doubles).
- Env: `CONFLUENCE_SPACE_KEY`, optional `CONFLUENCE_*` falling back to `JIRA_*`.

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Transport | Confluence Cloud REST (`/wiki/rest/api`) | Same auth style as Jira; MCP not used |
| Body format | Storage XHTML | Native Confluence representation |
| Attachments | REST child attachment API | HTML/JS survive sanitization better as files |
| Hierarchy | Parent → flow → run | Matches “same flow = subpages” product rule |

---

## Security techniques

| Control | Why |
|---------|-----|
| Soft-fail if misconfigured | Missing space/token never blocks analyse |
| Redact page bodies | Same secret hygiene as Jira comments |
| Reuse bot account + space ACL | Least surprise; grant add page + attachment only |
| No LLM | Nothing to leak into a model prompt |

---

## Performance techniques

| Technique | Why |
|-----------|-----|
| No LLM | Fixed cost: a few REST calls |
| Publish only on completed runs | Avoids junk pages for aborted smokes |
| Unique attachment names per timestamp | No overwrite fights on re-runs |
| Living “Latest” on flow page + immutable run children | Fast glance + full history |

---

## Related

- [Jira story worker](jira-story-worker.md) — may link the Confluence run URL on the ticket  
- [Transaction / analysts](../agents/overview.md) — supply metrics and TXNs in the page body  
- [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) — defines completed vs aborted  
- [Security](../security/security.md)  
