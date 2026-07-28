# Analysis QA agent

**Layman role:** Help desk — answers follow-ups about the last analysis **without** re-running the browser.

| | |
|--|--|
| **Code** | [`src/agents/analysis_qa_agent.py`](../../src/agents/analysis_qa_agent.py) |
| **Called from** | `answer_analysis_question` in [`src/nodes/routing.py`](../../src/nodes/routing.py) |
| **Prompt** | [`prompts/analysis_qa.txt`](../../prompts/analysis_qa.txt) |
| **LLM?** | Yes for Q&A (`TaskType.EXTRACTION`); rebuild path is deterministic |

← [All agents](overview.md)

---

## What it does

Uses existing `AgentState` (correlations, parameters, TXNs, artifacts) to answer questions like:

- “Which values are correlated for login?”  
- “What parameters were detected?”  

If the question mentions TXNs / k6 / script generation, it can **rebuild** transactions and regenerate IR/k6 from stored captures, then answer.

---

## Why it exists

Re-capturing for every follow-up is slow and flaky. In-thread Q&A keeps the Studio conversation useful after one successful run.

---

## How it works

1. Intent router selects `analysis_qa`.
2. Optional rebuild of TXN + k6 when keywords match.
3. Summarize bounded analysis context → LLM answer.
4. If LLM fails → rule-based fallback summary.

**Does not** open Playwright by design.

---

## Where it is used

```text
route_intent (analysis_qa) → answer_analysis_question → AnalysisQAAgent → END
```

Requires prior analysis context in the same LangGraph thread (or enough state fields populated).

---

## Technology

| Piece | Choice | Why |
|-------|--------|-----|
| LLM | Extraction-tier | Natural-language answers |
| Rebuild | TransactionAgent + IR/k6 helpers | Fresh artifacts without recapture |
| Formatters | `formatting.py` | Consistent playbook-style replies |

---

## Security techniques

- Context pack is **truncated** (~12k) before the LLM.
- Still may include sample parameter/correlation values — treat shared Studio threads carefully.
- No new navigation → URL policy not re-invoked for browsing.

---

## Performance techniques

- **No browser** — biggest win vs full pipeline.
- Rebuild only when the question needs TXN/k6.
- Short-circuits the graph (no orchestrate/capture/analyse loop).

---

## Related

- [Intent router](intent-router.md) — routes here  
- [Transaction agent](transaction-agent.md) — rebuild helper  
- [Shared state](agent-state.md) — what Q&A reads  
