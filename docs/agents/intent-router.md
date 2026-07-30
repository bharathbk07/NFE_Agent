# Intent router

**Layman role:** Thin gate — **assist vs execute** only. Sub-agent routing lives in the PE supervisor.

| | |
|--|--|
| **Code** | [`src/agents/intent_router.py`](../../src/agents/intent_router.py) |
| **Called from** | `route_intent` in [`src/nodes/routing.py`](../../src/nodes/routing.py) |
| **Prompt** | [`prompts/intent_classifier.txt`](../../prompts/intent_classifier.txt) |
| **LLM?** | Default for natural language (`TaskType.EXTRACTION`) |

← [All agents](overview.md) · [PE Assistant runtime](pe-assistant-runtime.md)

---

## Intents

| Intent | Meaning |
|--------|---------|
| `conversation` | Pure greetings / math |
| `pe_assist` | Personal PE assistant (supervisor + specialists) |
| `analysis_qa` | Legacy alias → same node as `pe_assist` |
| `watch_me` / `performance_analysis` / `follow_up_analysis` | Pipeline execute |
| `reuse_recording` | Load saved Watch-me |
| `jira_perf` | **Execute** Jira story worker only (`work on SCRUM-1`) |

Listing Jira stories → `pe_assist` (Integrations specialist via REST), not `jira_perf`.

## How it works

1. Mechanical whole-message commands (greetings, watch-me, work on KEY, list jira → pe_assist, URL payload)
2. LLM classifier for natural language
3. Promote open PE chat from `conversation` → `pe_assist`
4. Guard: questions cannot stay on pipeline execute intents
