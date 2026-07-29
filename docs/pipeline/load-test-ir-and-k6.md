# Load-Test IR → k6

How NFE turns analysed traffic into a **stable k6 script** without asking an LLM to write JavaScript.

This page is for operators and engineers who want to understand the blueprint (`*_ir.json`) and the deterministic emitter. Smoke validation and heal loops are covered in [Smoke + self-heal](smoke-and-self-heal.md).

---

## In one sentence

Agents classify parameters, correlations, and TXNs; **`build_load_test_ir`** builds a versioned JSON plan; **`generate_k6_script` / `emit_k6_from_ir`** compile that plan to k6 — **same IR → same script**.

---

## Why IR exists

LLMs are good at reasoning over journeys (what is a login, what looks like a CSRF, which fills are tester data). They are bad at authoring long, correct load scripts.

| Layer | Who | Role |
|-------|-----|------|
| Analyse agents | LLM + rules | Diff traffic, params vs correlations, TXN names |
| Load-Test IR | Deterministic | Structured plan: vars, extracts, requests, modes |
| k6 emit | Deterministic | Compiler: IR → JS (`k6/http` and optionally `k6/browser`) |
| Heal | Rule-based | Patch IR on script 4xx, re-emit (≤2) — not free-form LLM coding |

---

## Where it sits in the pipeline

Inside `analyse_traffic` ([`src/nodes/analyse.py`](../../src/nodes/analyse.py)):

```text
Traffic diff → ParameterAgent → CorrelationClassifier → TransactionAgent
        │
        ├─ build_load_test_ir(...)     → artifacts/k6/<app>/<flow>_ir.json
        ├─ generate_k6_script(ir=...)  → artifacts/k6/<app>/<flow>.js
        ├─ CLI k6 smoke / workload
        └─ heal_load_test_ir (≤2) → re-emit → re-run
```

Flow diagrams: [flow-diagrams.md](flow-diagrams.md).

---

## What the IR holds

Top-level shape from [`build_load_test_ir`](../../src/utils/load_test_ir.py) (`version: 1`):

| Field | Meaning |
|-------|---------|
| `target_url` / `origin` | Journey URL and origin for relative paths |
| `vars` | Tester-supplied / credential values (`name`, `value`, `is_credential`, optional `from_env`) |
| `correlations` | Extract → pass edges (CSRF, request ids, cookies flagged `auto_cookie`) |
| `transactions` | Ordered TXNs with `mode` (`protocol` or `browser`), requests, and optional `ui_steps` |
| `workload` | Optional load model: VUs, stages, **`pacing_s`**, **`think_time_s`**, thresholds |

### Think time, pacing, and per-TXN assertions

| Control | IR location | k6 behavior |
|---------|-------------|-------------|
| **Think time** | Per TXN `think_time_s: {min, max}` (default `1`–`3`). Scalar `1` still accepted. Workload `think_time_s` overrides all TXNs. | Emitted into `CONFIG.thinkTime`; TXNs call `sleep` from CONFIG. Set `NFE_THINK_TIME=0` to disable. |
| **Pacing** | `workload.pacing_s` (seconds, start-to-start). Omit or `0` = off. | `CONFIG.pacing_s`; at end of `default`, sleep remaining if `> 0`. |
| **Content assertion** | One request per protocol TXN marked `assertion_anchor` with an `assertion` object (`expect_status`, `body_contains` / `body_not_contains`, `json_path_exists`). | Extra k6 `check()` entries via `nfeAssertResponse`; failures mark the TXN failed. Other requests keep lightweight status/body checks. |

### Generated script layout (edit USER CONFIG only)

```text
1. imports
2. header comment
3. USER CONFIG — CONFIG (thinkTime, pacing_s, workload, thresholds) + vars
4. export const options  (reads CONFIG.workload / CONFIG.thresholds)
5. response callback + runtime helpers
6. TXN functions
7. export default + handleSummary
```

All human tunables (parameters, VUs, think time, pacing, thresholds) live in the **USER CONFIG** block at the top of the emitted `.js`.

### Assertion coverage gate (before smoke)

Before any k6 smoke/load run, NFE validates: **each protocol TXN has ≥1 valid content assertion** (N protocol TXNs ⇒ ≥N assertions). Browser TXNs are excluded from the count.

If coverage is short, NFE re-applies anchors (`apply_txn_anchor_assertions`), re-emits, and re-checks. If still short, **k6 is not started** and smoke reports `assertion coverage failed` (see [Smoke + self-heal](smoke-and-self-heal.md)).

Anchor selection (deterministic): correlation extract source → first mutating method → last XHR/document GET → last non-soft request. Markers come from the recording (`status` + `response_body`); dynamic values (ids, tokens, timestamps) are never asserted.

**IR builder also synthesizes common auth fixes** before emit:

- CSRF → `${csrf_token}` on auth/validate-style calls
- Create-resource id → `${requestId}` on follow-up GETs
- **Browser login** TXN (`synthesized: browser_login`, `mode: browser`, `sync_cookies_to_http: true`) when SPA session cannot be established with protocol HTTP alone

Credentials come from the Watch-me recording and/or chat/Jira `credentials:` (when `NFE_STORE_CREDENTIALS`). They land in `vars`, not as a global `NFE_USER`/`NFE_PASS` requirement.

---

## Protocol vs hybrid

| Mode | k6 APIs | When |
|------|---------|------|
| **Protocol** (default) | `k6/http` | Most API/XHR load; correlations via extract → `${var}` |
| **Browser** | `k6/browser` + Chromium | Narrow fallback—usually SPA **login**—then cookie sync back to HTTP |

Hybrid scripts skip redundant protocol “Launch” GETs when browser Login already opens the app, then run protocol TXNs for scale.

**Do not** scale with thousands of Chromium VUs. Hybrid is a correctness bridge; protocol HTTP is the load path. Timing differences (browser Login wall clock vs per-request HTTP) are explained in [Smoke + self-heal](smoke-and-self-heal.md#when-does-smoke-use-chromium).

---

## Data randomization (Run 2)

Before analyse diffs Run 1 vs Run 2, capture installs [`DataRandomizationMiddleware`](../../src/utils/data_randomization.py):

1. **Harvest** randomizable payload fields from Run 1
2. **Run 2** — `page.route` rewrites matching leaves so deliberate test-data changes are not mistaken for correlations
3. Ledger / non-randomizable routes feed the IR so the emitter can mock or flag third-party noise

Analyse filters the randomization ledger out of correlation candidates so only true server dynamics remain.

---

## Artifacts

| Path | Contents |
|------|----------|
| `artifacts/k6/<app>/<flow>_ir.json` | Load-Test IR (heal overwrites) |
| `artifacts/k6/<app>/<flow>.js` | Emitted k6 script (heal overwrites) |
| `artifacts/k6/<app>/html-report.html` | Last smoke/load HTML report |
| `artifacts/k6/<app>/summary.json` | k6 handleSummary |
| `artifacts/k6/<app>/k6-points.json` | `--out json` samples |

App / flow layout: [App artifacts & knowledge](app-artifacts-and-knowledge.md).

---

## Code map

| Piece | Location |
|-------|----------|
| Build IR | [`src/utils/load_test_ir.py`](../../src/utils/load_test_ir.py) — `build_load_test_ir` |
| Emit k6 | [`src/utils/k6_generator.py`](../../src/utils/k6_generator.py) — `generate_k6_script`, `emit_k6_from_ir` |
| Assertion gate | [`src/utils/k6_assertion_gate.py`](../../src/utils/k6_assertion_gate.py) — coverage check before smoke |
| Heal IR | [`src/utils/k6_healer.py`](../../src/utils/k6_healer.py) |
| Orchestration | [`src/nodes/analyse.py`](../../src/nodes/analyse.py) |
| Run 2 randomization | [`src/utils/data_randomization.py`](../../src/utils/data_randomization.py) |
| Artifact paths | [`src/utils/artifacts.py`](../../src/utils/artifacts.py) |

---

## Related docs

- [Flow diagrams](flow-diagrams.md)
- [Smoke + self-heal](smoke-and-self-heal.md)
- [App artifacts & knowledge](app-artifacts-and-knowledge.md)
- [Parameter agent](../agents/parameter-agent.md) · [Traffic analyst](../agents/traffic-analyst-agent.md) · [Transaction agent](../agents/transaction-agent.md)
