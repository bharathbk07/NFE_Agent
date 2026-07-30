# Trend / SLA RCA

> Explain performance trends or why SLA failed using local + Confluence evidence.

## When to use
- Trend report, Confluence sync, “why SLA failed”, p95/error-rate history

## Guidance
1. Resolve app/flow from context or question.
2. Prefer `sync_confluence_trends` when user wants Confluence / RAG refresh.
3. Else `get_run_trends` with exclude_smoke / min VUs if asked.
4. Answer with a KPI table, not page links alone.
5. `search_knowledge` for flow card / prior notes.

## Constraints
- Never dump Confluence URLs as the only answer.
