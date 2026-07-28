# Traffic analyst agent

**Layman role:** Forensic accountant for HTTP — finds what **changed** between two runs.

| | |
|--|--|
| **Code** | [`src/agents/analyst_agent.py`](../../src/agents/analyst_agent.py) |
| **Called from** | `analyse_traffic` in [`src/nodes/analyse.py`](../../src/nodes/analyse.py) |
| **Prompt** | None |
| **LLM?** | **No** — fully deterministic |

← [All agents](overview.md)

---

## What it does

Compares network logs from Run 1 vs Run 2 (and optionally Run 3):

- Which cookies, headers, query params, body fields, or path IDs **differ**?  
- Which earlier **response** likely produced a value used in a later **request**? (extract → pass)

Those dynamic values become **correlation candidates** for the load-test IR.

---

## Why it exists

**Correlation** is the hardest part of performance scripting. Doing it with rules (not an LLM writing k6) keeps results stable, reviewable, and free of invented extractors.

---

## How it works

1. Align request pairs by method / path / step index.
2. Diff values across runs; drop noise (fingerprint headers, cache-busters, etc.).
3. Trace origin of dynamic values in prior response bodies/headers where possible.
4. Emit `correlations` and `dependencies` into `AgentState`.

Also looks for stable server IDs that appear again in later paths (create → `/resource/{id}`).

---

## Where it is used

```text
Capture (2× runs) → analyse_traffic
                       → TrafficAnalystAgent
                       → ParameterAgent
                       → CorrelationClassifierAgent (optional LLM polish)
                       → TransactionAgent → IR → k6
```

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Differential analysis | Pure Python over CDP/Playwright logs | Repeatable, no hallucination |
| Noise filters | `correlation_noise` helpers | Fewer false “dynamic” tokens |
| State | Typed `CorrelationItem` / `DependencyChain` | Clean handoff to IR builder |

---

## Security techniques

- Runs entirely on **already captured** local traffic — no new network to arbitrary hosts.
- Focuses on actionable auth/CSRF-like surfaces; reduces dumping irrelevant client noise into scripts.
- Does not call LLMs (nothing to leak into a prompt).

---

## Performance techniques

- No LLM latency or token cost.
- Pairwise alignment + early noise filters shrink work for later stages.
- Optional Run 3 is decided elsewhere (correlation classifier), not inside this agent.

---

## Related

- [Parameter agent](parameter-agent.md) — tester-supplied values (opposite of server dynamics)  
- [Correlation classifier](correlation-classifier-agent.md) — LLM second opinion  
- [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) — fixes remaining CSRF/ID script bugs after emit  
