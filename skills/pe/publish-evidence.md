# Publish evidence

> Persist PE outcomes: Jira comment, optional analysis issue, Confluence sync/publish cues.

## When to use
- User asks to publish findings, update the story, or open an analysis ticket.
- After a failed run when create-on-fail was authorized.

## Constraints
- Default on fail: `comment_jira_issue` on the parent story.
- `create_jira_issue` only when authorized + `NFE_JIRA_CREATE_ENABLED`.
- Prefer structured RCA text from `format_run_report` / trends tools.

## Suggested Hands
1. `format_run_report`
2. `comment_jira_issue` and/or `create_jira_issue`
3. `sync_confluence_trends` when trend evidence is needed
