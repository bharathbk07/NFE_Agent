# NFE agents — overview

Plain-language map of planning/analysis agents under [`src/agents/`](../../src/agents/).

**Product outcome:** an Agentic AI assistant for Performance Testing & Engineering — understand the app (stories / journeys / BA context), create scripts, run them, analyze results, and surface RCA / fix guidance. Delivery workers (Jira + Confluence) publish that evidence.

**Jira and Confluence are workers** (REST delivery), not `src/agents/` classes — see [`docs/workers/overview.md`](../workers/overview.md).

**Dedicated agent pages** (what / why / how / where / tech / security / performance):

| Agent | Doc | Layman role |
|-------|-----|-------------|
| Intent router | [intent-router.md](intent-router.md) | Receptionist — what does the user want? |
| Orchestrator | [orchestrator-agent.md](orchestrator-agent.md) | Project manager — split the journey into phases |
| Navigator | [navigator-agent.md](navigator-agent.md) | Browser choreographer — click/fill plans |
| Traffic analyst | [traffic-analyst-agent.md](traffic-analyst-agent.md) | Forensic HTTP diff — what changed? |
| Parameter agent | [parameter-agent.md](parameter-agent.md) | What the tester typed → variables |
| Correlation classifier | [correlation-classifier-agent.md](correlation-classifier-agent.md) | Second opinion — param vs correlation |
| Transaction agent | [transaction-agent.md](transaction-agent.md) | Name business TXNs for reports/k6 |
| Analysis QA / PE Assistant | [analysis-qa-agent.md](analysis-qa-agent.md) · [pe-assistant-runtime.md](pe-assistant-runtime.md) | PE Agent OS (Brain + Hands + Skills); supervisor fallback |
| Shared state | [agent-state.md](agent-state.md) | Clipboard shared by all nodes |

Graph wiring: [`src/graph.py`](../../src/graph.py) · Nodes: [`src/nodes/`](../../src/nodes/)  
Also: [Documentation index](../README.md) · [Workers](../workers/overview.md) · [Security](../security/security.md) · [Smoke & self-heal](../pipeline/smoke-and-self-heal.md) · [App artifacts & knowledge](../pipeline/app-artifacts-and-knowledge.md)

---

## Big picture

```text
Chat message
    │
    ▼
Intent router ── assist ──► PE Supervisor (specialists)
    │                         KnowledgeQA / Evidence / Integrations / Scripting
    │
    ├─► jira execute (`work on KEY`) → Jira worker
    └─► watch-me / analyse / reuse → pipeline workers
              │
              ▼
Traffic analyst → Parameter → Correlation → Transaction
    → IR → k6 → smoke → heal
              │
              └─► Confluence worker (if run fully completed)
```

**Design rule:** LLMs judge and plan. Diffs, grouping, script emit, heal, Jira comments, and Confluence pages stay **rule-based / REST** so outputs don’t hallucinate.

---

## Shared technology

| Layer | Technology | Why |
|-------|------------|-----|
| Orchestration | LangGraph + `AgentState` | Multi-step workflow with shared state |
| LLM | LangChain + model router | Task routing (orchestration / navigation / extraction) |
| Browser | Playwright + CDP | Real clicks + protocol-grade capture |
| Load scripts | k6 CLI + deterministic IR | Same analysis → same script |
| Workers | Jira + Confluence REST | Chat pickup + durable reports |
| Security | [`src/security/`](../../src/security/) | URL/step policy, secret placeholders, path jail |

**Cross-cutting performance:** heuristics before LLM; truncate evidence; avoid browser when possible; deterministic IR→k6.  
**Cross-cutting security:** credential placeholders; URL/step allowlists; redact artifacts/comments; fail closed on policy errors.

---

## Tech matrix (agents)

| Agent | LLM? | Deterministic core? | Opens browser? |
|-------|------|---------------------|----------------|
| Intent router | Fallback | Heuristics first | No |
| Orchestrator | Yes* | Fallback task | No |
| Navigator | Yes | Fallback navigate/wait | Plans only |
| Traffic analyst | No | Yes | Consumes captures |
| Parameter agent | No | Yes | Consumes captures |
| Correlation classifier | Yes | Cookie fallback | May request Run 3 |
| Transaction agent | No | Yes | Consumes captures |
| Analysis QA | Yes (Q&A) | TXN/k6 rebuild | No |

\*Orchestrator LLM skipped on Watch-me.

Workers (no LLM for delivery): [Jira](../workers/jira-story-worker.md) · [Confluence](../workers/confluence-publisher-worker.md)

---

## Prompt map

| Prompt | Agent doc |
|--------|-----------|
| `intent_classifier.txt` | [Intent router](intent-router.md) (also routes to Jira worker) |
| `orchestrator_task_decomposer.txt` | [Orchestrator](orchestrator-agent.md) |
| `navigator_agent_step_planner.txt` | [Navigator](navigator-agent.md) |
| `correlation_classifier.txt` | [Correlation classifier](correlation-classifier-agent.md) |
| `analysis_qa.txt` | [Analysis QA](analysis-qa-agent.md) |
| `browser_self_heal.txt` | Playwright tool (see [Navigator](navigator-agent.md)) |
| *(none)* | [Workers](../workers/overview.md) — template + REST only |
