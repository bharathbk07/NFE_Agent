# Correlation classifier agent

**Layman role:** Second opinion — “Is this a parameter or a correlation?” when rules are unsure.

| | |
|--|--|
| **Code** | [`src/agents/correlation_classifier_agent.py`](../../src/agents/correlation_classifier_agent.py) |
| **Called from** | `analyse_traffic` after deterministic pair analysis |
| **Prompt** | [`prompts/correlation_classifier.txt`](../../prompts/correlation_classifier.txt) |
| **LLM?** | Yes (`TaskType.EXTRACTION`), with deterministic fallback |

← [All agents](overview.md)

---

## What it does

After Traffic analyst + Parameter agent finish, this agent:

- Advises which UI fills are really **correlations** (or stay parameters)  
- Comments on cookie / session handling  
- Can request a **third capture (Run 3)** when evidence is weak  

`apply_correlation_advice` then promotes LLM-marked items out of the parameter list when needed.

---

## Why it exists

Pure diffs miss SPA edge cases (silent login 200, ambiguous fills). A bounded LLM review improves quality **without** letting the model write the k6 script — the IR compiler still emits code.

---

## How it works

1. Build a **truncated evidence pack** (fills, cookies, dynamics, short response snippets).
2. LLM returns structured `CorrelationAdvice`.
3. Apply advice to state; optionally run Playwright Run 3 once, then re-analyse.
4. If LLM fails → cookie-diff **deterministic fallback**.

---

## Where it is used

```text
analyse_traffic
  → deterministic analysts
  → CorrelationClassifierAgent
       ├─ advice only
       └─ needs_extra_run → Playwright Run 3 → re-diff → classify again (once)
  → TransactionAgent → IR → k6
```

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| LLM | Extraction-tier model | Classification, not code gen |
| Structured advice | Pydantic / schema | Safe to apply mechanically |
| Fallback | Cookie diffs | Pipeline continues offline |

---

## Security techniques

| Control | Why |
|---------|-----|
| Truncate URLs, bodies, values | Limits secret/PII exposure to the model |
| Credentials as **keys only** | Never paste password values into the classifier prompt |
| Cap list sizes | Smaller blast radius if logs leak |

---

## Performance techniques

- Evidence caps (e.g. fills ≤40, cookies ≤40, dynamics ≤25, snippets ≤8).
- Run 3 is optional and runs at most once in the loop — the expensive path.
- Failover / skip LLM still yields usable cookie notes.

---

## Related

- [Traffic analyst](traffic-analyst-agent.md) · [Parameter agent](parameter-agent.md)  
- [Navigator](navigator-agent.md) / capture — Run 3 uses the same automation stack  
- [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) — remaining auth/ID fixes after script emit  
