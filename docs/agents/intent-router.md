# Intent router

**Layman role:** Receptionist — figures out what you want before NFE starts expensive work.

| | |
|--|--|
| **Code** | [`src/agents/intent_router.py`](../../src/agents/intent_router.py) |
| **Called from** | `route_intent` in [`src/nodes/routing.py`](../../src/nodes/routing.py) — **first** graph step |
| **Prompt** | [`prompts/intent_classifier.txt`](../../prompts/intent_classifier.txt) |
| **LLM?** | Only when heuristics are ambiguous (`TaskType.EXTRACTION`) |

← [All agents](overview.md)

---

## What it does

Classifies the latest chat message into one intent, for example:

| Intent | Meaning |
|--------|---------|
| `conversation` | Small talk / help — skip the pipeline |
| `analysis_qa` | Question about a prior analysis in this thread |
| `watch_me` | You will click; agent records |
| `performance_analysis` / `follow_up_analysis` | Bot plans and drives the browser |
| `reuse_recording` | Re-analyse a saved Watch-me file |
| `jira_perf` | Process a Jira story (`work on SCRUM-1`) |

It can also stash a Jira issue key when it finds `PROJECT-123` in the text.

---

## Why it exists

Without routing, every “hi” or follow-up question could open Chromium and burn LLM tokens. The router keeps cheap requests cheap and only starts capture/analyse when needed.

---

## How it works

1. **Fast path (no LLM):** regex / keyword heuristics for greetings, watch-me phrases, Jira phrases, reuse-recording, URLs, prior-analysis Q&A.
2. **Slow path:** if still unclear, one structured LLM call returns `IntentDecision` (intent + optional short reply).

Graph branching is in `after_intent_router` → conversation, analysis QA, orchestrate, load recording, or Jira node.

---

## Where it is used

```text
START → route_intent → (branch by intent)
```

Every Studio / CLI chat turn starts here.

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| Heuristics | Python regex | Fast, predictable, free |
| LLM fallback | Model router extraction task | Handles messy natural language |
| Structured output | Pydantic `IntentDecision` | Reliable downstream branching |

---

## Security techniques

- Truncates user text (~4k chars) before sending to the LLM.
- Does not browse URLs or open the browser.
- Extracts Jira keys only; no credentials required at this stage.

---

## Performance techniques

- Heuristics first → most common phrases never call an LLM.
- At most one classification call per turn.
- Conversation intent ends the graph immediately.

---

## Related

- [Orchestrator](orchestrator-agent.md) — next for journey planning  
- [Analysis QA](analysis-qa-agent.md) — when intent is `analysis_qa`  
- [Jira integration](../workers/jira-integration.md) — `jira_perf` path  
