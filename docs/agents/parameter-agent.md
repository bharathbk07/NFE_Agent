# Parameter agent

**Layman role:** Finds what **you typed** — values that should become load-test variables.

| | |
|--|--|
| **Code** | [`src/agents/parameter_agent.py`](../../src/agents/parameter_agent.py) |
| **Called from** | `analyse_traffic` (with Traffic analyst) |
| **Prompt** | None |
| **LLM?** | **No** — deterministic |

← [All agents](overview.md)

---

## What it does

Scans UI fill/select steps and matches those values onto HTTP requests. Marks candidates such as:

- username / password (credentials)  
- form fields (remarks, amounts, names)  

Outputs `parameterizable_candidates` for the IR (`vars.*` in k6).

It **skips** fields that look like runtime correlations (session tokens, CSRF, server IDs).

---

## Why it exists

In load testing, **parameters** (tester-chosen) and **correlations** (server-generated) must not be mixed. Parameters are what each VU may vary; correlations must be extracted every iteration.

---

## How it works

1. Walk journey steps for fills/selects.
2. Find the same value on the wire (body / query / header).
3. Classify with helpers (credential vs normal field vs correlation-like).
4. Record selector, variable name, propagations.

---

## Where it is used

```text
analyse_traffic
  TrafficAnalystAgent  → dynamics / dependencies
  ParameterAgent       → vars / credentials
  reconcile + CorrelationClassifier → cleanup
  → IR → k6
```

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Matching | Deterministic step ↔ request scan | Same input → same vars |
| Classification helpers | Shared perf-test rules | Consistent with IR builder |

---

## Security techniques

- Identifies credential fields so they can stay as env-backed vars (`NFE_USER` / `NFE_PASS`) instead of literals.
- Does **not** send password values to an LLM.
- Helps keep secrets out of the “correlation” bucket (wrong place for passwords).

---

## Performance techniques

- Linear scan; skips static assets and empty/placeholder fills early.
- No LLM cost.
- Small candidate lists keep IR and k6 generation fast.

---

## Related

- [Traffic analyst](traffic-analyst-agent.md) — server-side dynamics  
- [Correlation classifier](correlation-classifier-agent.md) — may reclassify a fill as correlation  
- [Security](../security/security.md) — credential storage / redaction defaults  
