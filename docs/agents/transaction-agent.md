# Transaction agent

**Layman role:** Labels the journey like a performance engineer — Login, Create Claim, Submit, …

| | |
|--|--|
| **Code** | [`src/agents/transaction_agent.py`](../../src/agents/transaction_agent.py) |
| **Called from** | Late `analyse_traffic`; also Analysis QA when rebuilding TXNs |
| **Prompt** | None in code (deterministic). `prompts/transaction_grouper.txt` exists but is **unused** today |
| **LLM?** | **No** |

← [All agents](overview.md)

---

## What it does

Groups captures into **business transactions** with:

- human-readable name / description  
- UI steps  
- HTTP requests that belong to that TXN  

Used in chat reports, HTML smoke report, and k6 grouping/metrics tags.

---

## Why it exists

Load testers think in **transactions** (like JMeter/NeoLoad controllers), not raw URL lists. Clean TXNs make SLAs and failure analysis understandable.

---

## How it works

Deterministic grouping using, in preference order:

1. Orchestrator sub-task phases  
2. Watch-me **Start TXN / End TXN** overlay markers  
3. URL / login heuristics  

Then filters noise: telemetry hosts, static assets, SPA chrome GETs, typeahead spam — so scripts stay business-focused.

---

## Where it is used

```text
analyse_traffic → … → TransactionAgent → build_load_test_ir → k6
Analysis QA (TXN/k6 questions) → may rebuild transactions + script
```

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Grouping | Pure Python heuristics | Stable names across runs |
| IR / k6 | TXN names become groups + `nfe_txn_*` metrics | Report + scale analysis |

---

## Security techniques

- Drops third-party analytics / tracker hosts from the script surface.
- Does not invent URLs; only rearranges captured traffic.
- No LLM → no extra prompt leakage.

---

## Performance techniques

- No model latency.
- Caps leftover GETs; collapses revisited shells for readable IR.
- Faster k6 emit and clearer HTML reports.

---

## Related

- [Orchestrator](orchestrator-agent.md) — phase names often become TXNs  
- [Analysis QA](analysis-qa-agent.md) — can rebuild TXNs on demand  
- [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) — TXN timing in reports  
