# Analysis QA agent

**Layman role:** Legacy session Q&A helpers — context pack / rebuild used by the PE supervisor specialists.

| | |
|--|--|
| **Code** | [`src/agents/analysis_qa_agent.py`](../../src/agents/analysis_qa_agent.py) |
| **Runtime** | Prefer [PE Assistant runtime](pe-assistant-runtime.md) (`pe_assist` → supervisor) |
| **Called from** | Fallback inside `answer_analysis_question`; context helpers used by specialists |
| **Prompt** | [`prompts/analysis_qa.txt`](../../prompts/analysis_qa.txt) (legacy); specialists use `prompts/agents/*` |

← [All agents](overview.md)

Studio chat assist path now runs the **multi-sub-agent PE supervisor**. This module still builds the session/knowledge context pack and optional TXN/k6 rebuild.


---

## What it does

Uses existing `AgentState` (correlations, parameters, TXNs, artifacts, **smoke results**) plus **local knowledge markdown + Chroma RAG** to answer questions like:

- “Which values are correlated for login?”  
- “What parameters were detected?”  
- “Did smoke pass? What’s the p95?”  
- “Show trend across recent runs”

If the question mentions TXNs / k6 / script generation, it can **rebuild** transactions and regenerate IR/k6 from stored captures, then answer.

### Cache-first retrieval (trends / results)

```text
1. Session state (k6_smoke, artifact paths, Confluence URLs already in thread)
2. Knowledge markdown (flow card + runs/<flow>_*.md history)
3. Chroma RAG over knowledge
4. Tool refresh (Confluence sync → write back to knowledge/RAG) only on miss
   or explicit “from Confluence / refresh”; monitoring stub for future tools
```

Chat does **not** hit Confluence or monitoring on every question.

---

## Why it exists

Re-capturing for every follow-up is slow and flaky. In-thread Q&A keeps the Studio conversation useful after one successful run.

---

## How it works

1. Intent router selects `analysis_qa`.
2. Optional rebuild of TXN + k6 only when the user clearly asks to regenerate the script.
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

- Context pack is **truncated** (~12k) and passed through `redact_text_for_llm` before the LLM.
- Run-history markdown is redacted before disk/RAG upsert.
- Still may include sample parameter/correlation values — treat shared Studio threads carefully.
- No new navigation → URL policy not re-invoked for browsing.
- Confluence refresh (when used) stays under the configured space / NFE parent hierarchy — no arbitrary page URLs from chat.

---

## Performance techniques

- **No browser** — biggest win vs full pipeline.
- Rebuild only when the question needs TXN/k6.
- Short-circuits the graph (no orchestrate/capture/analyse loop).

---

## Related

- [Overview](overview.md)
- [Smoke + self-heal](../pipeline/smoke-and-self-heal.md)
- [App artifacts & knowledge](../pipeline/app-artifacts-and-knowledge.md) — run history + RAG ladder
- Helpers: [`perf_trend.py`](../../src/utils/perf_trend.py), [`perf_evidence.py`](../../src/utils/perf_evidence.py), [`knowledge_store.ingest_run_history`](../../src/utils/knowledge_store.py)

- [Intent router](intent-router.md) — routes here  
- [Transaction agent](transaction-agent.md) — rebuild helper  
- [Shared state](agent-state.md) — what Q&A reads  
