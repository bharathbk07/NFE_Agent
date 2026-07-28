# Navigator agent

**Layman role:** Browser choreographer — turns each journey phase into click/fill/wait steps.

| | |
|--|--|
| **Code** | [`src/agents/navigator_agent.py`](../../src/agents/navigator_agent.py) |
| **Called from** | `plan_navigator_steps` in [`src/nodes/capture.py`](../../src/nodes/capture.py) |
| **Prompt** | [`prompts/navigator_agent_step_planner.txt`](../../prompts/navigator_agent_step_planner.txt) |
| **LLM?** | Yes (`TaskType.NAVIGATION`) |

← [All agents](overview.md)

---

## What it does

For each orchestrator sub-task, outputs a list of **Playwright-style steps**:

- navigate to a URL  
- fill / type into fields  
- click / select  
- wait for load or selectors  

It plans the script; **capture nodes** actually open Chromium and run it (twice for differential analysis).

---

## Why it exists

Humans describe intent in English (“log in and create a claim”). Automation needs precise actions and selectors. The navigator bridges that gap without letting the LLM write the final k6 load script.

---

## How it works

1. LLM returns structured `PlaywrightStep` / `StepPlanResponse`.
2. Credential placeholders are substituted with real values at execution time.
3. Steps pass through **step policy** (`filter_allowed_steps`) — unsafe actions are dropped.
4. Navigate URLs pass **URL policy** (`assert_url_allowed`).
5. On planning failure → minimal fallback (navigate + wait).
6. Optional LangSmith Hub prompt when `USE_LANGSMITH_PROMPTS=true`.

**Not used for Watch-me recording** — you click; no step planner needed for Run 1.

---

## Where it is used

```text
orchestrate_journey → plan_navigator_steps → NavigatorAgent
                            → run_automation (Run 1 + Run 2)
                            → analyse_traffic
```

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| LLM | Navigation-oriented model routing | Selector/step planning |
| Execution | Playwright (separate tool) | Real browser + CDP capture |
| Optional Hub | LangSmith prompt pull | Central prompt ops when enabled |

### Related “self-heal” (not this agent)

If a selector fails **during** capture, [`playwright_tool.py`](../../src/tools/playwright_tool.py) may call `prompts/browser_self_heal.txt` with a truncated DOM/a11y snapshot. That is **selector self-heal**, different from [k6 smoke heal](../pipeline/smoke-and-self-heal.md).

---

## Security techniques

| Control | Why |
|---------|-----|
| URL allow/deny before navigate | Blocks SSRF-style private/metadata hosts |
| Playwright action allowlist | Only safe actions (no arbitrary code) |
| Credential placeholders in the LLM prompt | Secrets stay out of model context |
| Truncated self-heal HTML/a11y | Limits data sent to the heal LLM |

---

## Performance techniques

- One LLM plan per sub-task; can skip if steps already planned.
- Dedupes redundant navigates.
- Fallback plan avoids infinite replan loops.
- Dual headless runs happen in capture — navigator itself does not open the browser.

---

## Related

- [Orchestrator](orchestrator-agent.md) — supplies sub-tasks  
- [Traffic analyst](traffic-analyst-agent.md) — consumes the captures this plan produces  
- [Security](../security/security.md) — URL and step policy details  
