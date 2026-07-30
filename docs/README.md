# NFE Agent documentation

Index of project docs. Start with the root [`README.md`](../README.md) for product vision, setup, and end-to-end flows.

## Product outcome

NFE is an **Agentic AI assistant for Performance Testing & Engineering**:

1. Understand the application (BA / Jira stories / recorded journeys — BA-doc depth expanding)
2. Create performance scripts (deterministic IR → k6)
3. Run smoke and load
4. Analyze results (HTML, SLA, TXN/request tables)
5. Deliver **RCA signals and fix guidance** (heal notes, 4xx vs 5xx, Jira + Confluence findings)

MVP focus: Watch-me / Navigator / Jira → k6 → reports. Full BA-document ingestion is on the roadmap; story AC + recordings are the current understanding inputs.

## Agents

Overview: [agents/overview.md](agents/overview.md)

| Agent | Doc |
|-------|-----|
| Intent router | [agents/intent-router.md](agents/intent-router.md) |
| Orchestrator | [agents/orchestrator-agent.md](agents/orchestrator-agent.md) |
| Navigator | [agents/navigator-agent.md](agents/navigator-agent.md) |
| Traffic analyst | [agents/traffic-analyst-agent.md](agents/traffic-analyst-agent.md) |
| Parameter agent | [agents/parameter-agent.md](agents/parameter-agent.md) |
| Correlation classifier | [agents/correlation-classifier-agent.md](agents/correlation-classifier-agent.md) |
| Transaction agent | [agents/transaction-agent.md](agents/transaction-agent.md) |
| Analysis QA | [agents/analysis-qa-agent.md](agents/analysis-qa-agent.md) |
| PE Assistant runtime | [agents/pe-assistant-runtime.md](agents/pe-assistant-runtime.md) |
| Shared state | [agents/agent-state.md](agents/agent-state.md) |

## Workers

Overview: [workers/overview.md](workers/overview.md)

| Worker / guide | Doc |
|----------------|-----|
| Jira story worker | [workers/jira-story-worker.md](workers/jira-story-worker.md) |
| Confluence publisher worker | [workers/confluence-publisher-worker.md](workers/confluence-publisher-worker.md) |
| Jira setup & ops | [workers/jira-integration.md](workers/jira-integration.md) |
| Confluence setup & ops | [workers/confluence-publishing.md](workers/confluence-publishing.md) |

## Pipeline & quality

| Doc | Description |
|-----|-------------|
| [**Flow diagrams**](pipeline/flow-diagrams.md) | User flow, end-to-end pipeline, data transmission (Mermaid) |
| [Load-Test IR → k6](pipeline/load-test-ir-and-k6.md) | Deterministic IR blueprint, protocol/hybrid emit, Run 2 randomization |
| [Smoke check and self-heal](pipeline/smoke-and-self-heal.md) | k6 smoke gate, heal loop, catastrophic abort (≥60% fail), Chromium vs HTTP |
| [App artifacts & knowledge](pipeline/app-artifacts-and-knowledge.md) | Domain-scoped recordings/k6, markdown knowledge, local ChromaDB RAG |

## Security

| Doc | Description |
|-----|-------------|
| [Security](security/security.md) | Threat model, URL/step/fs policy, **per-app credential store**, comment redaction |

## MCP (optional)

| Doc | Description |
|-----|-------------|
| [Optional MCPs](mcp/optional-mcps.md) | Project MCP registry (`config/mcp_servers.json`); k6 / Playwright / Atlassian |

## Folder layout

```text
docs/
├── README.md
├── agents/          # LLM / analysis agents
├── workers/         # Jira + Confluence workers and setup guides
├── pipeline/
├── security/
└── mcp/
```
