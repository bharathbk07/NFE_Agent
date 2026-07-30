# Smoke validate

> Run local k6 smoke / assertion gate against the current script artifact.

## When to use
- User asks to smoke-test, validate script, or re-check after heal.
- After IR→k6 emit and before publishing.

## Suggested Hands
1. `run_local_k6_smoke`
2. `format_run_report`
3. On fail → heal notes in report; optionally reload `trend-sla-rca` skill
