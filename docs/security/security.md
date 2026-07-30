# Security

## Threat model

NFE Agent is a **single-operator desktop** workflow (LangGraph Studio + CLI).  
Chat messages and visited page content are treated as **untrusted**. The agent can open a browser, call LLMs, write under `artifacts/`, and run k6.

**In scope controls:** SSRF-style navigation limits, secret handling for LLMs/logs/disk, Playwright action allowlists, recording/artifact path jails, capture redaction.

**Out of scope (today):** multi-tenant auth for Studio, encrypting artifacts at rest, OS-level Chromium network namespaces.

## Defaults

| Setting | Default | Purpose |
|---------|---------|---------|
| `NFE_URL_DENY_PRIVATE` | `true` | Block RFC1918 / link-local / metadata hosts |
| `NFE_ALLOW_LOCALHOST` | `false` | Set `true` only for local demo apps |
| `NFE_URL_ALLOWLIST` | empty | If set (comma hosts), only those hosts |
| `NFE_STORE_CREDENTIALS` | `true` | Persist Watch-Me username/password into recordings, IR, and k6 scripts (per app). Required for multi-app demos without per-app env vars. |
| `NFE_REDACT_ARTIFACTS` | `true` | Mask auth headers/cookies in network captures; password **fills** are kept when `NFE_STORE_CREDENTIALS=true` |
| `NFE_SELF_HEAL_HTML_CHARS` | `15000` | Truncate DOM sent to self-heal LLM |
| `NFE_SELF_HEAL_A11Y_CHARS` | `6000` | Truncate a11y JSON for self-heal |

## Operator surface

- Run Studio bound to **localhost** only. Do not expose `langgraph dev` on `0.0.0.0` without an auth proxy.
- App login credentials come from the **Watch-Me recording** (and optional chat/Jira credentials), not from a global `NFE_USER`/`NFE_PASS`. That scales across apps.
- k6 scripts embed those values as `vars.password` / `vars.username`. Heal LLMs may see them; **Jira/Confluence comments** still redact password-like text.
- Set `NFE_STORE_CREDENTIALS=false` only if you must wipe secrets from disk (scripts will then have empty passwords unless you recover another way).

## Jira (chat-driven)

- Require label `nfe-agent` before processing (Studio chat or CLI).
- Comments are sanitized via [`src/integrations/jira/security.py`](../../src/integrations/jira/security.py) (password/token redaction).
- Story `target_url` passes through URL policy before runs.
- Atlassian MCP stays disabled by default; story processing uses REST only. See [`jira-integration.md`](../workers/jira-integration.md).

## Error handling

NFE uses a typed exception hierarchy in [`src/exceptions.py`](../../src/exceptions.py):

| Class | Behavior |
|-------|----------|
| `NFESecurityError` (URL / step / fs jail) | **Fail closed** — never bypassed |
| `NFEConfigError` / `NFEAuthError` | **Fail closed** — fix env / credentials |
| `NFEPipelineError` / `NFEValidationError` | Soft-fail → `error_log` + chat message |
| `NFEIntegrationError` | Soft result-dict / chat; auth subclass hard |

**Security rules for errors:**

- User-facing messages and Jira comments are **redacted** (no passwords, tokens, or stack traces).
- Unexpected exceptions are logged with `logger.exception` (or equivalent), then soft-failed on pipeline nodes.
- Policy violations (`UrlPolicyError`, `StepPolicyError`, `FsJailError`) always re-raise.

## What is enforced in code

- [`src/exceptions.py`](../../src/exceptions.py) — typed errors, redacted user messages, hard vs soft failure helpers
- [`src/security/url_policy.py`](../../src/security/url_policy.py) — `page.goto` / navigate steps
- [`src/security/step_policy.py`](../../src/security/step_policy.py) — action allowlist
- [`src/security/secrets.py`](../../src/security/secrets.py) — LLM placeholders + redaction
- [`src/security/fs_jail.py`](../../src/security/fs_jail.py) — recordings + k6 artifact filenames
- [`src/integrations/jira/`](../../src/integrations/jira/) — comment sanitization, label gate

MCP servers in [`config/mcp_servers.json`](../../config/mcp_servers.json) stay **disabled** by default; npm packages are version-pinned (no `@latest`).

Studio (`langgraph dev`) should stay localhost-only.
