# Build script from recording

> Reuse a saved Watch-me / journey recording, emit Load Test IR → deterministic k6, then smoke-validate.

## When to use
- User asks to generate/rebuild a script from an existing recording.
- `list_recordings` shows a usable file for the app/flow.

## Constraints
- Never invent k6 source — IR→k6 only.
- Prefer `reuse_recording` handoff then analyse pipeline; use `run_local_k6_smoke` after emit.

## Suggested Hands (guidance, not a fixed pipeline)
1. `list_recordings`
2. `reuse_recording` (confirm if needed)
3. After analyse artifacts exist → `run_local_k6_smoke`
4. `format_run_report`
