# Shared state (`AgentState`)

**Layman role:** Shared clipboard — every node reads and writes the same run notebook.

| | |
|--|--|
| **Code** | [`src/agents/state.py`](../../src/agents/state.py) |
| **Called from** | All LangGraph nodes (not an executable agent) |
| **Prompt** | None |
| **LLM?** | No |

← [All agents](overview.md)

---

## What it is

TypedDict schemas for the LangGraph workflow, including:

| Area | Example fields |
|------|----------------|
| Chat | `messages` |
| Routing | `intent`, `jira_issue_key`, `recording_mode` |
| Journey | `target_url`, `credentials`, `user_journey_steps`, `sub_tasks` |
| Captures | `run_records` (Run 1 / 2 / 3) |
| Analysis | `correlations`, `dependencies`, `parameterizable_candidates`, `transactions` |
| Randomization | `randomization_ledger`, `non_randomizable_endpoints` |
| Delivery | `performance_test_output`, `recording_file`, `error_log` |

Supporting types: `NetworkRequestLog`, `RunRecord`, `CorrelationItem`, `DependencyChain`, `ParameterCandidate`, `TransactionGroup`, etc.

---

## Why it exists

Studio chat spans many nodes and turns. A single typed state keeps intent, captures, and k6 artifacts consistent without ad-hoc globals.

---

## How it is used

- Nodes return **partial updates**; LangGraph merges them (messages use `add_messages`).
- Agents read what they need and write results back through their calling node.
- Analysis QA answers from whatever analysis fields are already present.

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| TypedDict | Static structure | Clear contracts between agents |
| `add_messages` | LangGraph reducer | Append-only chat history |

---

## Security techniques

- May hold credentials **in memory** for the active run.
- Disk persistence of secrets controlled by `NFE_STORE_CREDENTIALS` and artifact redaction (`NFE_REDACT_ARTIFACTS`).
- State itself enforces no policy — [`src/security/`](../../src/security/) does at action boundaries.

---

## Performance techniques

- Schema-only — negligible CPU.
- Large `run_records` dominate memory; keep runs focused and noise-filtered upstream.

---

## Related

- [Intent router](intent-router.md) — sets `intent`  
- [All analyst agents](overview.md) — fill analysis fields  
- [Security](../security/security.md) — credential / redaction defaults  
