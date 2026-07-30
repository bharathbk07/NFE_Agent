# Run performance story (+ analysis ticket on fail)

> When the user wants you to pick/run a performance Jira story and optionally open an analysis issue if results fail.

## When to use
- “Work on the performance user story…”
- “If it fails create a Jira issue for analysis”
- Similar NL without an exact SCRUM-n key

## Guidance (not a hard-coded script)
1. `rank_jira_stories` with the user goal (or `list_jira_stories` then reason).
2. If `ambiguous` → ask the user; wait for confirmation / key.
3. `execute_jira_story` for the chosen key (requires execute authorization or confirm).
4. `format_run_report` from session / tool output.
5. If failed AND user authorized create-analysis → `create_jira_issue` with RCA description + acceptance criteria; mention parent key.
6. Optionally `sync_confluence_trends` if evidence pages matter.

## Constraints
- Do not use `list_jira_stories` alone as the final answer for this goal.
- Do not invent a new issue if create is disabled — show a draft instead.
