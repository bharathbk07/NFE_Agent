# Intent router

**Layman role:** Receptionist — understands what you *mean* before NFE starts expensive work.

| | |
|--|--|
| **Code** | [`src/agents/intent_router.py`](../../src/agents/intent_router.py) |
| **Called from** | `route_intent` in [`src/nodes/routing.py`](../../src/nodes/routing.py) — **first** graph step |
| **Prompt** | [`prompts/intent_classifier.txt`](../../prompts/intent_classifier.txt) |
| **LLM?** | Default path for natural language (`TaskType.EXTRACTION`) |

← [All agents](overview.md)

---

## What it does

Classifies the latest chat message into one intent by **meaning**, for example:

| Intent | Meaning |
|--------|---------|
| `conversation` | Small talk / help — skip the pipeline |
| `analysis_qa` | Question about prior analysis in this thread |
| `watch_me` | You will click; agent records |
| `performance_analysis` / `follow_up_analysis` | Bot plans and drives the browser |
| `reuse_recording` | Re-analyse a saved Watch-me file |
| `jira_perf` | User **explicitly asks to execute** a Jira story (`work on SCRUM-1`) |

Mentions of “Jira”, issue keys, smoke, k6, Confluence, etc. are **topics**, not automatic triggers.

---

## Why it exists

Without routing, every “hi” or follow-up question could open Chromium and burn LLM tokens. Without *understanding*, keyword hits like “jira” would start the wrong pipeline — which is not sellable product behavior.

---

## How it works

1. **Mechanical fast path (no LLM):** only ultra-clear whole-message commands — greetings, math, bare URL/JSON payloads, `watch me …`, `list recordings`, `work on SCRUM-1` / `run jira`.
2. **Natural-language path (LLM):** everything else. Soft signals (detected phrases) are passed as **hints only**; the classifier decides from meaning.
3. **Safety guard:** if the LLM returns a pipeline intent (`jira_perf`, new analysis, rerun) but the message is clearly a question, it is downgraded to `analysis_qa` (with prior context) or `conversation`.
4. **Fail closed:** on LLM failure, never auto-run Jira/pipeline — prefer QA or conversation.

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
| Mechanical commands | Narrow regex on whole messages | Free, only for unambiguous product commands |
| NLU classifier | Model router extraction task | Understands messy natural language |
| Structured output | Pydantic `IntentDecision` | Reliable downstream branching |
| Question guard | Post-check on pipeline intents | Stops accidental Jira/analysis runs |

---

## Security techniques

- Truncates user text (~4k chars) before sending to the LLM.
- Does not browse URLs or open the browser.
- Extracts Jira keys only when intent is already `jira_perf`; no credentials at this stage.

---

## Performance techniques

- Mechanical path skips the LLM for greetings and explicit commands.
- One classification call per natural-language turn.
- Conversation / QA intents end or stay cheap; pipelines only on clear execute intent.

---

## Related

- [Orchestrator](orchestrator-agent.md) — next for journey planning  
- [Analysis QA](analysis-qa-agent.md) — when intent is `analysis_qa`  
- [Jira integration](../workers/jira-integration.md) — `jira_perf` path  
