# NFE workers — overview

Workers are **deterministic REST / delivery pipelines** (not LLM planner classes under `src/agents/`). They pick up external work or publish **performance evidence** (results, RCA signals, artifacts) after analyse.

They support the product outcome: understand → script → run → analyze → **file findings** on Jira/Confluence for the team.

| Worker | Doc | Layman role |
|--------|-----|-------------|
| Jira story | [jira-story-worker.md](jira-story-worker.md) | Ticket runner — story → pipeline → Jira comment |
| Confluence publisher | [confluence-publisher-worker.md](confluence-publisher-worker.md) | Report librarian — completed run → Confluence page + attachments |

### Setup & ops guides (same folder)

| Guide | Doc |
|-------|-----|
| Jira Cloud setup, labels, tokens, troubleshooting | [jira-integration.md](jira-integration.md) |
| Confluence publish rules, env, hierarchy | [confluence-publishing.md](confluence-publishing.md) |

Core planning/analysis agents: [`docs/agents/overview.md`](../agents/overview.md)  
Index: [`docs/README.md`](../README.md) · Main: [`README.md`](../../README.md)

```text
docs/workers/
├── overview.md                      ← this file
├── jira-story-worker.md
├── confluence-publisher-worker.md
├── jira-integration.md
└── confluence-publishing.md
```
