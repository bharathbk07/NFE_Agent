# PE Agent OS (OpenClaw-inspired)

**Layman role:** Always-on junior PE lead — given a plain-language goal, the Brain decides which Hands to use to Create, Run, Analyze, and Publish. Skills teach *how*; automation scripts do not replace decision loops.

| | |
|--|--|
| **Runtime** | [`src/agents/runtime/pe_agent.py`](../../src/agents/runtime/pe_agent.py) |
| **Hands** | [`hands.py`](../../src/agents/runtime/hands.py) + [`hands_registry.py`](../../src/agents/runtime/hands_registry.py) |
| **Skills** | [`skills/pe/*.md`](../../skills/pe/) |
| **Approval** | [`exec_approval.py`](../../src/agents/runtime/exec_approval.py) |
| **Lane** | [`lane.py`](../../src/agents/runtime/lane.py) |
| **Memory** | [`memory.py`](../../src/agents/runtime/memory.py) |
| **Heartbeat** | [`heartbeat.py`](../../src/agents/runtime/heartbeat.py) |
| **Node** | `answer_analysis_question` in [`src/nodes/routing.py`](../../src/nodes/routing.py) |
| **Prompt** | [`prompts/agents/pe_agent.txt`](../../prompts/agents/pe_agent.txt) |
| **Flag** | `NFE_PE_AGENT_ENABLED` (default true; falls back to legacy supervisor) |

← [All agents](overview.md)

---

## Architecture

```text
Channels (Studio / CLI heartbeat)
    → Gateway (session lane + exec approval)
         → PE Agent Runtime (ReAct, TaskType.ASSIST)
              ├─ Brain (model router)
              ├─ Hands (Playwright/Watch-me, k6, Jira, Confluence, knowledge)
              ├─ Skills (Markdown playbooks, loaded on demand)
              └─ Memory (thread notes + knowledge/RAG)
```

Legacy specialists ([`supervisor.py`](../../src/agents/runtime/supervisor.py)) remain as fallback when `NFE_PE_AGENT_ENABLED=false`.

Mechanical shortcuts (`work on SCRUM-1`, `watch me`) still enqueue the same domain workers.

## Capability stages (chosen dynamically)

| Stage | Example Hands |
|-------|----------------|
| Create | `request_watch_me`, `list_recordings`, `reuse_recording` |
| Run | `execute_jira_story`, `run_local_k6_smoke` |
| Analyze | `format_run_report`, `get_run_trends`, `sync_confluence_trends`, `search_knowledge` |
| Publish | `comment_jira_issue`, `create_jira_issue` |

IR→k6 stays deterministic — the Brain never invents k6 source.

## Exec Approval

Risky Hands (`execute` / `mutate` with confirm) wait for sticky `pending_action` unless the thread already authorized that class (e.g. user said “if fail, create analysis issue”).

## Heartbeat

Off by default (`NFE_HEARTBEAT_ENABLED=false`).

```bash
python -m src.agents.runtime.heartbeat --once
```

Eligible stories / unfinished jobs → propose; otherwise `HEARTBEAT_OK`.

## Adding a Skill

1. Add `skills/pe/<id>.md` with title + `>` description line
2. Restart / next turn — catalog picks it up
3. Agent calls `load_skill` when relevant — **no new intent regex**

## Adding a Hand

1. Implement in `build_default_hands`
2. Set `RiskTier` + `auth_keys` / confirm flags
3. Prefer bridging existing workers (`process_issue_key`, Confluence sync, etc.)

## Channels

- **Studio** — LangGraph thread state carries `pe_thread_id`, `pending_action`, `agent_authorizations`
- **CLI** — heartbeat module (`make heartbeat`); story execute remains Jira workers

## Security

- Secret redaction on tool I/O
- Create gated by `NFE_JIRA_CREATE_ENABLED` + approval
- URL/step/fs jail unchanged
- Serial lane per thread (no racing Watch-me + Jira on same state)

## Legacy specialist docs

Specialist registry / MCP adapters still apply when using supervisor fallback — see git history of this file for the old multi-specialist diagram.
