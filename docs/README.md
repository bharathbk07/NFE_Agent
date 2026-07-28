# NFE Agent documentation

Index of project docs. Start with the root [`README.md`](../README.md) for setup and end-to-end flows.

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
| [Smoke check and self-heal](pipeline/smoke-and-self-heal.md) | k6 smoke gate, heal loop, Chromium vs HTTP timing |

## Security

| Doc | Description |
|-----|-------------|
| [Security](security/security.md) | Threat model, URL/step/fs policy, secrets, exceptions |

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
