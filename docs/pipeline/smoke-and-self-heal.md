# Smoke check and self-heal

This document explains the **auto-check + self-heal** step that runs after NFE generates a k6 script—what it does, what it does *not* do, and how to read the results.

It is written for product managers, SDEs, and performance engineers. Code pointers are at the end.

---

## In one sentence

After the agent builds a load script, it **runs a tiny k6 test**. If that test fails because of **script mistakes** (stale tokens, missing login, wrong IDs), NFE **rewrites the script plan and tries again**—up to twice. If the **application** is broken (server errors), it leaves that visible and does not paper over it.

---

## Why this exists

A recording can look perfect while the first generated script still fails:

| What went wrong | Typical symptom |
|-----------------|-----------------|
| Login CSRF copied as a fixed string from the recording | Later API calls get **401 Unauthorized** |
| Create-claim ID hard-coded from Run 1 | Next call to `/requests/8` gets **403/404** |
| Extra SPA “chrome” GETs (menus, assets) | Noisy failures that are not the business journey |
| Missing login step in the IR | Every protected API fails |

Humans used to fix these by hand in JMeter/NeoLoad/k6. NFE’s heal loop applies the **same kinds of fixes automatically**, using **rules**—not an LLM rewriting 2,000 lines of JavaScript.

---

## Where it sits in the pipeline

```text
Capture (Watch-me / Navigator / saved recording)
        │
        ▼
Analyse traffic → Build Load-Test IR → Generate k6 script
        │
        ▼
┌───────────────────────────────────────┐
│  SMOKE + HEAL (this document)         │
│                                       │
│  1. Save script + IR under artifacts/ │
│  2. Run: k6 run <script>              │
│     (default: 1 virtual user × 2 iters)│
│  3. If fail → heal IR (rules)         │
│  4. Regenerate k6 (overwrite same file)│
│  5. Smoke again (max 2 heal attempts) │
│  6. Build HTML report + chat summary  │
└───────────────────────────────────────┘
        │
        ▼
Chat summary / optional Jira Test Report comment
```

Triggered from the `analyse_traffic` node after IR → k6 emit ([`src/nodes/analyse.py`](../../src/nodes/analyse.py)).

---

## What “smoke” means here

**Smoke** = a cheap correctness check, not a load test.

| Setting | Typical value | Purpose |
|---------|---------------|---------|
| Virtual users | 1 | One user path |
| Iterations | 2 | Catch flaky script bugs twice |
| Tool | CLI `k6 run` | Same engine you use in CI |
| Credentials | `NFE_USER` / `NFE_PASS` env | Not baked into the script by default |

If `k6` is not installed, smoke is **skipped** (script is still written; chat notes that validation did not run).

### Threshold watcher (`abortOnFail`)

k6 uses **layered** thresholds (Grafana k6 best practice):

| Layer | Example | Effect |
|-------|---------|--------|
| **SLA** (end of test) | `http_req_failed` `rate<0.01`, p95, checks `rate>0.99` | Marks the run **failed** when breached; does **not** stop early by default |
| **Catastrophic abort** | fail rate ≥ **60%**, extreme p99, checks collapse | **`abortOnFail: true`** — stops the test mid-run and marks it **failed** |

```ini
NFE_K6_ABORT_ON_FAIL=true
NFE_K6_ABORT_DELAY=10s
NFE_K6_ABORT_FAIL_RATE=0.60
NFE_K6_ABORT_P99_MS=30000
NFE_K6_ABORT_CHECKS_MIN=0.40
NFE_K6_SLA_ABORT_ON_FAIL=false
```

After `delayAbortEval`, if ≥60% of requests have failed (`http_req_failed` rate ≥ `NFE_K6_ABORT_FAIL_RATE`), k6 aborts. Same for p99 above `NFE_K6_ABORT_P99_MS` or checks pass-rate below `NFE_K6_ABORT_CHECKS_MIN`.

NFE marks the run **failed**, tags `aborted_by_watcher`, and still **publishes Confluence** when `summary.json` exists (`COMPLETED — WATCHER STOPPED`) — unless the 4xx script gate skips. Infrastructure timeouts / spawn failures without a summary do **not** publish.

**Default smoke** (empty workload) does **not** use `abortOnFail`, so self-heal can see the full failure set.

Outputs:

| Artifact | Role |
|----------|------|
| `artifacts/k6/<host>.js` | Script (overwritten on each heal) |
| `artifacts/k6/<host>_ir.json` | Structured plan the compiler reads |
| `artifacts/k6/html-report.html` | Human-readable TXN / fail view |
| `artifacts/k6/k6-points.json` | Raw samples used to build the HTML report |
| Chat / Jira | Pass/fail + short heal notes |

---

## Script bug vs application failure

Generated scripts treat HTTP status codes differently on purpose:

| HTTP status | Interpreted as | Smoke impact |
|-------------|----------------|--------------|
| **2xx / 3xx** | Success | Pass |
| **4xx** (401, 403, 404, …) | Usually a **script** problem (auth, correlation, wrong URL/id) | **Fail** — healer may fix |
| **5xx** | **Application** fault (server/app outage) | **Allowed** — not “healed away” |
| Network / blocked (status 0) | Connectivity / policy | Fail |

**Plain language:**  
If the *test script* is wrong, NFE tries to fix the script.  
If the *app under test* is returning server errors, that stays visible so you do not ship a green script that hides production pain.

Implementation: `http.expectedStatuses` and check helpers in [`src/utils/k6_generator.py`](../../src/utils/k6_generator.py) / [`src/utils/k6_runtime_helpers.js`](../../src/utils/k6_runtime_helpers.js).

---

## The heal loop (step by step)

1. **First smoke** runs on the freshly generated script.
2. If exit code is **0** → done. Chat says smoke passed.
3. If smoke **failed** (and was not skipped):
   - Call `heal_load_test_ir(ir, smoke_result, attempt)` ([`src/utils/k6_healer.py`](../../src/utils/k6_healer.py)).
   - That function returns an updated **IR** plus human-readable **heal notes**.
   - Regenerate k6 from the new IR.
   - **Overwrite** the same script/IR filenames (stable artifacts for CI).
   - Run smoke again.
4. Repeat until smoke passes or **2 heal attempts** are exhausted.
5. Results (including `heal_notes`) go into `performance_test_output.k6_smoke` and the chat playbook.

```text
attempt 0: generate → smoke
             │ fail
attempt 1: heal IR → regenerate → smoke
             │ fail
attempt 2: heal IR → regenerate → smoke
             │
           stop (pass or still failing)
```

---

## What the healer actually changes

Healing is **deterministic** (if/then rules on smoke signals). It does **not** ask an LLM to rewrite the k6 file.

### A. Auth / session (login tokens)

**Signal:** smoke shows **401** / Unauthorized on APIs.

**Typical fixes:**

1. Force CSRF correlation — replace a stale captured `_token` with `${csrf_token}` extracted from the login HTML.
2. Inject a missing login POST if the IR had APIs but no auth step.
3. If 401 **persists** after CSRF is already wired → convert **Login** to **browser mode** (k6/browser opens a real Chromium login, syncs cookies into the HTTP jar, then the rest of the journey stays protocol HTTP).

That last step is the “hybrid” path: browser only for login when pure HTTP cannot establish a session. See [When does smoke use Chromium?](#when-does-smoke-use-chromium) and [How response time is calculated](#how-response-time-is-calculated).

### B. Dynamic resource IDs

**Signal:** **403/404** on paths like `/requests/{id}` after a create.

**Typical fix:** Extract the create response’s `data.id` into `${requestId}` and use that variable on downstream URLs—so VU 2 does not reuse Run 1’s hard-coded id.

### C. Noise reduction

- Drop non-critical SPA “chrome” GETs (menus, static shells) that are not the business API.
- On later attempts, relax status checks on **non-critical** GETs only (never on business-critical APIs—those must stay strict so real 401/404 are not hidden).
- Deduplicate colliding correlation variable names.
- Prefer alternate JSON extract paths when extracts look empty.
- Coalesce noisy typeahead intermediate GETs.

### D. When nothing matches

If rules cannot map the failure to a known pattern, heal notes say roughly: *No deterministic heal applied — review failed checks and correlations.* The script remains a draft; open the HTML report.

---

## How to read results in chat / Jira

| Message | Meaning |
|---------|---------|
| Smoke **passed** | Script survived the tiny run; safe to think about scaling VUs later |
| Smoke **failed** + heal notes | Fixes were attempted; open HTML report for URL + status |
| Smoke **skipped** | `k6` missing or not run; install k6 and re-analyse |
| “Smoke passed after heal attempt N” | First script was wrong; rule-based fix made smoke green |
| “401 persisted: converted Login to browser mode” | Protocol login was not enough; hybrid login is now in the script |
| “4xx = script bug / 5xx = app” (in script comments) | Reminder of the pass/fail policy above |

Scale load (many VUs) **only after smoke is green**. Smoke is a gate, not a capacity test.

---

## When does smoke use Chromium?

**Smoke does not open Chromium to re-test product logic.** Its job is: “Does this **generated k6 script** work?”

| Phase | Chromium? | Purpose |
|-------|-----------|---------|
| **Watch-Me / Navigator** (capture) | Yes (Playwright) | You or the bot click the real UI; NFE records traffic to *build* the script |
| **Smoke (typical)** | **No** | Replay the journey as `k6/http` requests only |
| **Smoke (hybrid login fallback)** | **Yes, login TXN only** | Establish a real browser session, sync cookies into the HTTP jar, then continue with protocol APIs |

### Normal smoke (no browser)

```text
Smoke = k6/http only → login POST + APIs as network calls
         (no Chromium)
```

### Hybrid smoke (rare)

Used only after heal sees **persistent 401** and protocol CSRF/login is not enough (common on some SPAs: login returns HTTP 200 but you are still not really logged in).

```text
Smoke = Chromium ONLY for Login TXN → sync cookies → rest of journey as k6/http
```

Analogy:

- **Watch-Me Chromium** = filming the journey  
- **Smoke HTTP** = replaying the edited tape as fast network calls  
- **Smoke Chromium (rare)** = only re-doing “unlock the door” when the tape’s fake key does not work  

Chromium in smoke is a **session bootstrap**, not a full UI test and not how you scale to hundreds of VUs.

---

## How response time is calculated

NFE records custom metrics (`nfe_txn_duration`, `nfe_req_duration`, …) that feed the HTML report. Protocol and browser TXNs are timed differently.

### Protocol TXN (most of the journey)

```text
Start timer → http.get / http.post / … → Stop timer → nfeMarkTxn
```

You get:

1. **Per-request time** — k6’s `res.timings.duration` (true network time for that call), tagged with TXN / URL  
2. **TXN time** — wall clock from the start to the end of that TXN’s HTTP steps (`nfe_txn_duration`)

Protocol TXNs also wrap work in k6 `group(txnName, …)` for grouping in standard k6 output.

### Browser TXN (hybrid Login only)

k6 `group()` does not support async browser callbacks, so the generated script marks timing manually:

```text
__nfeTxnStart = Date.now()
  page.goto / type credentials / click / wait until off /auth/login …
nfeMarkTxn(name, start, failed)   // elapsed = Date.now() - start
```

For a **browser Login** TXN, duration is roughly:

> open page + type + click + wait until navigation leaves the login URL

That wall-clock sample **includes**:

- Network under the hood (page load, XHR the browser fires)  
- Chromium rendering / JS  
- Waiting for selectors and navigation  

It does **not** currently break out “POST /auth/validate took 45 ms” the way protocol mode does. Those calls happen inside Chromium; NFE does not export each of them as separate `nfe_req_duration` rows for that TXN.

After login, cookies are synced and **later TXNs stay protocol**—those keep normal per-request timings.

### How to read the HTML report

| TXN type | What “response time” means |
|----------|----------------------------|
| **Browser Login** | End-to-end **user login** time (heavier; includes browser) |
| **Protocol Create Claim / Submit / …** | HTTP steps in that TXN (+ per-URL times) |

**Do not** compare browser Login p95 to a pure HTTP Login p95 as the same metric.

For **load / capacity**, focus on the **protocol** TXNs after login. Browser Login timing answers: “Did the session establish, and how long did that cost?”—not the main scale path.

Implementation: [`src/utils/k6_generator.py`](../../src/utils/k6_generator.py) (`_emit_protocol_txn`, `_emit_browser_txn`), [`src/utils/k6_runtime_helpers.js`](../../src/utils/k6_runtime_helpers.js) (`nfeMarkTxn`), [`src/utils/k6_report_builder.py`](../../src/utils/k6_report_builder.py).

---

## Two different “self-heals” (do not confuse them)

| Feature | When | What it fixes | Uses LLM? |
|---------|------|---------------|-----------|
| **k6 smoke heal** (this doc) | After script generation | Auth, CSRF, create-IDs, noisy GETs in the **load script** | **No** — rules on IR |
| **Browser selector self-heal** | During Watch-me / Navigator clicks | Broken CSS/role selectors when the UI changed | **Yes** — small DOM/a11y prompt |

Both improve reliability; only the smoke heal is about **parameterization / correlation** in the load script.

---

## What smoke + heal will *not* do

- Guarantee a perfect script on every application (MVP: complex correlations still need review).
- Fix a broken product that returns systematic **5xx**.
- Replace a full performance test (no ramp, no SLO campaign)—only a correctness gate.
- Use Chromium to re-validate every UI screen or “test app logic” in the browser during smoke.
- Give per-request HTTP timings *inside* a browser Login TXN (only wall-clock TXN duration today).
- Upload results to Grafana Cloud / your CI by itself (artifacts are local; you wire CI).
- Invent business logic the journey never performed.

---

## Example story (OrangeHRM-style)

1. You Watch-me login → create claim → submit.
2. NFE builds IR + k6.
3. **Smoke 1 fails:** APIs return 401 because `_token` was a literal from the recording.
4. **Heal 1:** wire `${csrf_token}` from login HTML into auth/validate.
5. **Smoke 2 fails:** create works, but `/requests/8` 404s (stale id).
6. **Heal 2:** correlate create `data.id` → `${requestId}`.
7. **Smoke 3 passes.** HTML report shows green TXNs. Chat lists the heal notes.

If after two heals login still 401s because the SPA never exposes a usable CSRF on the wire, heal converts Login to browser mode and retries.

---

## Operator checklist

1. Install k6: [Install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/).
2. For authenticated apps, export credentials for the smoke process:

   ```bash
   NFE_USER=Admin NFE_PASS=secret
   ```

3. After a run, open `artifacts/k6/html-report.html` if smoke failed.
4. Read heal notes in the Studio chat summary (or Jira Test Report).
5. Only raise VUs / iterations in the IR or script after smoke is green.

---

## Code map

| Piece | Location |
|-------|----------|
| Orchestrates smoke + ≤2 heals | [`src/nodes/analyse.py`](../../src/nodes/analyse.py) |
| Rule-based IR fixes | [`src/utils/k6_healer.py`](../../src/utils/k6_healer.py) |
| CLI `k6 run` + status enrichment | [`src/utils/k6_runner.py`](../../src/utils/k6_runner.py) |
| Prefer CLI for smoke (HTML/points) | [`src/utils/k6_mcp.py`](../../src/utils/k6_mcp.py) |
| IR → k6 emit (incl. expected statuses) | [`src/utils/k6_generator.py`](../../src/utils/k6_generator.py) |
| Auth / CSRF / create-id helpers | [`src/utils/load_test_ir.py`](../../src/utils/load_test_ir.py) |
| HTML report from points | [`src/utils/k6_report_builder.py`](../../src/utils/k6_report_builder.py) |
| Chat playbook smoke section | [`src/utils/formatting.py`](../../src/utils/formatting.py) |
| Browser *selector* self-heal (different) | [`src/tools/playwright_tool.py`](../../src/tools/playwright_tool.py), [`prompts/browser_self_heal.txt`](../../prompts/browser_self_heal.txt) |

Related docs: [`security.md`](../security/security.md), [`jira-integration.md`](../workers/jira-integration.md), main [`README.md`](../../README.md).
