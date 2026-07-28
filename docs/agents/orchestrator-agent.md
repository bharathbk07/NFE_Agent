# Orchestrator agent

**Layman role:** Project manager — splits a long user journey into ordered phases.

| | |
|--|--|
| **Code** | [`src/agents/orchestrator_agent.py`](../../src/agents/orchestrator_agent.py) |
| **Called from** | `orchestrate_journey` in [`src/nodes/orchestrate.py`](../../src/nodes/orchestrate.py) |
| **Prompt** | [`prompts/orchestrator_task_decomposer.txt`](../../prompts/orchestrator_task_decomposer.txt) |
| **LLM?** | Yes (`TaskType.ORCHESTRATION`), except Watch-me |

← [All agents](overview.md)

---

## What it does

Takes a natural-language journey (or structured steps) and produces **sub-tasks** such as:

- authentication  
- navigation  
- form input  
- transaction / submit  

Each sub-task has a name, description, and focus so the navigator can plan one phase at a time.

---

## Why it exists

One giant “do the whole checkout” prompt is hard for models and hard to debug. Smaller phases produce better Playwright plans and cleaner transaction grouping later.

---

## How it works

1. LLM returns structured JSON (`SubTaskPlanResponse` / `SubTaskSpec` list).
2. If the journey is empty or the LLM fails → single fallback task `main_flow`.
3. **Watch-me path:** LLM is **skipped**. A hard-coded `watch_me_flow` sub-task is used because *you* drive the browser.

Credentials in the prompt are **placeholders** (not real passwords).

---

## Where it is used

```text
route_intent → orchestrate_journey → OrchestratorAgent
                    │
        ┌───────────┴───────────┐
   Navigator path          Watch-me path
   (plan steps)            (no LLM here)
```

Runs for `performance_analysis` / `follow_up_analysis`. Not used as an LLM step for watch-me.

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| LLM | Stronger “reasoning” models via router | Decomposition needs judgment |
| Structured output | JSON schema / RobustJson fallback | Stable handoff to navigator |
| LangGraph state | Writes `sub_tasks` into `AgentState` | Shared with later nodes |

---

## Security techniques

- Sends `credentials_placeholders(...)` to the model — keys like `username` / `password`, not secret values.
- Does not navigate or call external apps itself.

---

## Performance techniques

- One planning call per run (or **zero** for watch-me).
- Model failover on 503/timeout via the model router.
- Avoids re-orchestration when a watch-me recording path is already selected.

---

## Related

- [Navigator](navigator-agent.md) — turns each sub-task into Playwright steps  
- [Transaction agent](transaction-agent.md) — often mirrors these phases in TXN names  
- [Intent router](intent-router.md) — decides whether orchestration is needed  
