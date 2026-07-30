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
| Smoke/load **passed** (`smoke.ok=true`) with summary | **Yes** |
| Smoke/SLA **failed** (any reason) | **No** (`smoke_failed_no_publish` / `script_4xx_failures` / …) |
| Watcher abort / incomplete / skipped / missing script | **No** |

Failures and correlation/script bugs stay on the **Jira story comment** only. Confluence is reserved for **green** evidence so the space is not littered with false “completed” runs.

Gate: `should_publish_to_confluence()` / `explain_confluence_skip()` in [`src/integrations/confluence/publisher.py`](../../src/integrations/confluence/publisher.py). Skip reasons include `smoke_failed_no_publish`, `script_4xx_failures`, `no_space_key`, `missing_confluence_credentials`, `incomplete_no_summary`, `smoke_skipped`, etc.

---

## Setup

### 1. Bot permissions

Same Atlassian account as Jira (recommended). In Confluence, grant the bot on the target space:

- View
- Add/edit pages
- Add attachments

**Auth:** HTTP Basic with **Confluence email** + **Confluence API token** (classic / unscoped recommended).

### 2. `.env`

```ini
# Prefer explicit Confluence credentials (falls back to JIRA_* if empty)
CONFLUENCE_BASE_URL=https://your-site.atlassian.net
CONFLUENCE_EMAIL=bot@example.com
CONFLUENCE_API_TOKEN=your_confluence_api_token
CONFLUENCE_SPACE_KEY=ENG
CONFLUENCE_PARENT_TITLE=Performance Testing and Engineering
NFE_CONFLUENCE_PUBLISH=true
```

Restart `langgraph` / Studio after changing `.env` (settings load at import).

If `CONFLUENCE_SPACE_KEY` is empty, publish is disabled, or credentials are missing after Jira fallback, NFE **skips** Confluence (soft-fail — analyse still succeeds).

### 3. Attachments

Uploaded on each **Run** page:

- k6 script (`.js`)
- HTML smoke report (`.html`)
- Load-Test IR (`.json`, optional)

Filenames are unique per flow + timestamp so re-runs do not clash.

Run pages include **planned/actual VUs**, **TPS / HTTP req rate**, workload model, and workload source (`jira_story` vs `default_smoke`).

### Run page content (HTML-parity)

Storage body mirrors the local HTML k6 report ([`src/integrations/confluence/report.py`](../../src/integrations/confluence/report.py)):

1. Coloured status lozenge (PASS / SLA FAILED / script issues)
2. KPI strip (duration, reqs, iterations, error rate, p95, failed buckets, TPS, VUs)
3. General test details + observation notes
4. Full TXN table (min/max/avg/count/failed/percentiles)
5. Full request table
6. Failed request list (from `k6-points.json` when present)
7. SLA thresholds with PASS/FAIL macros
8. Failed checks, heal notes, attachment list

Points are resolved from `points_json` on the smoke result, or `k6-points.json` next to the k6 script.

---

## Wiring

- **Studio / CLI:** after smoke + heal in [`analyse_traffic`](../../src/nodes/analyse.py).
- **Jira path:** analyse sets `skip_k6_smoke` + `skip_confluence_publish`; [`run_perf_for_request`](../../src/integrations/jira/pipeline.py) merges the **story workload**, emits k6 once, runs that script, then publishes Confluence. The Jira comment includes the Confluence URL (or the skip reason).

---

## Page status labels

| Label | Meaning |
|-------|---------|
| `PASSED` | Completed + thresholds ok (or none) + smoke ok |
| `COMPLETED — SLA FAILED` | Completed but thresholds failed |
| `COMPLETED — WATCHER STOPPED` | Threshold `abortOnFail` stopped the run early (summary present) |
| `COMPLETED — CHECKS/SCRIPT ISSUES` | Completed but smoke checks failed |
| `COMPLETED — NO SLA` | Completed with no thresholds and inconclusive smoke |

---

## Troubleshooting

| Symptom | Check |
|---------|--------|
| Nothing published | `CONFLUENCE_SPACE_KEY`, `NFE_CONFLUENCE_PUBLISH`, chat/Jira skip reason, `summary.json` present |
| Skip reason `script_4xx_failures` | Expected for broken-script 4xx-heavy runs; fix correlation/script then re-run |
| Page updated despite many 4xx | Should no longer happen — confirm latest publisher; check skip reason in chat/Jira |
| Thin Confluence body (no TXN tables) | Ensure `k6-points.json` exists beside the script / smoke returns `points_json` |
| `missing_confluence_credentials` | Set `CONFLUENCE_EMAIL` + `CONFLUENCE_API_TOKEN` (or Jira fallbacks); restart Studio |
| 401/403 | Token email, space permissions, classic token vs site URL |
| No attachments | Files exist under `artifacts/k6/<app>/`; bot can add attachments |
| Wrong flow page | Recording file name / `recording` in Jira story config |

See also: [`jira-integration.md`](jira-integration.md), [`confluence-publisher-worker.md`](confluence-publisher-worker.md), [`security.md`](../security/security.md).
