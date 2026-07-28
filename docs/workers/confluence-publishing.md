# Confluence publishing

NFE can publish **completed** performance/smoke results to **Confluence Cloud** — with the HTML report and k6 script as attachments. Publishing is **deterministic** (templates + REST). No LLM is used.

---

## Hierarchy

```text
Space (CONFLUENCE_SPACE_KEY)
  └── Performance Testing and Engineering     ← fixed parent (find-or-create)
        ├── {Flow name}                       ← Watch-me / recording name
        │     └── Run YYYY-MM-DD HH:MM        ← every completed run (child)
        └── {Other flow}
```

- **Flow identity** = recording / Watch-me file stem (e.g. `Create Claim`), else recording hint, else host from `target_url`.
- The **flow page** is updated with a “Latest run” summary after each publish.
- Each run also gets an immutable **dated child** page with full stats + attachments.

---

## When results are published

| Outcome | Publish? |
|---------|----------|
| Test **fully completed**, SLA/thresholds **failed** | **Yes** |
| Test **fully completed**, SLA/thresholds **passed** | **Yes** |
| Test **fully completed**, **no SLA** defined | **Yes** |
| Test **stopped mid-run** (timeout, abort, crash, k6 missing/skipped) | **No** |

“Fully completed” means k6 finished the planned scenario (default smoke: 1 VU × 2 iterations, or story workload). Failed checks or crossed thresholds still count as a completed run with a report.

Gate: `should_publish_to_confluence()` in [`src/integrations/confluence/publisher.py`](../../src/integrations/confluence/publisher.py).

---

## Setup

### 1. Bot permissions

Same Atlassian account as Jira (recommended). In Confluence, grant the bot on the target space:

- View
- Add/edit pages
- Add attachments

Prefer a **classic / unscoped** API token with site URL + Basic auth (same as Jira).

### 2. `.env`

```ini
# Empty BASE/EMAIL/TOKEN → fall back to JIRA_*
CONFLUENCE_BASE_URL=https://your-site.atlassian.net
CONFLUENCE_EMAIL=
CONFLUENCE_API_TOKEN=
CONFLUENCE_SPACE_KEY=ENG
CONFLUENCE_PARENT_TITLE=Performance Testing and Engineering
NFE_CONFLUENCE_PUBLISH=true
```

If `CONFLUENCE_SPACE_KEY` is empty or publish is disabled, NFE **skips** Confluence (soft-fail — analyse still succeeds).

### 3. Attachments

Uploaded on each **Run** page:

- k6 script (`.js`)
- HTML smoke report (`.html`)
- Load-Test IR (`.json`, optional)

Filenames are unique per flow + timestamp so re-runs do not clash.

---

## Wiring

- **Studio / CLI:** after smoke + heal in [`analyse_traffic`](../../src/nodes/analyse.py).
- **Jira path:** analyse skips Confluence; [`run_perf_for_request`](../../src/integrations/jira/pipeline.py) publishes after the final workload smoke, then the Jira comment includes the Confluence URL when published.

Mid-run failures still get a detailed **Why it failed / stopped** Jira comment (no Confluence page).

---

## Page status labels

| Label | Meaning |
|-------|---------|
| `PASSED` | Completed + thresholds ok (or none) + smoke ok |
| `COMPLETED — SLA FAILED` | Completed but thresholds failed |
| `COMPLETED — CHECKS/SCRIPT ISSUES` | Completed but smoke checks failed |
| `COMPLETED — NO SLA` | Completed with no thresholds and inconclusive smoke |

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Nothing published | `CONFLUENCE_SPACE_KEY`, `NFE_CONFLUENCE_PUBLISH`, k6 completed (`summary.json` present) |
| 401/403 | Token email, space permissions, classic token vs site URL |
| No attachments | Files exist under `artifacts/k6/`; bot can add attachments |
| Wrong flow page | Recording file name / `recording` in Jira story config |

See also: [`jira-integration.md`](jira-integration.md), [`confluence-publisher-worker.md`](confluence-publisher-worker.md), [`security.md`](../security/security.md).
