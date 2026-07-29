# Jira integration (chat-driven REST)

NFE Agent can pick up **Jira Cloud** stories labeled **`nfe-agent`**, check for a Watch-me recording, run the load-test pipeline, and post result comments — **from LangGraph Studio chat**.

| Path | Role |
|------|------|
| **Studio chat** (`jira_perf` intent → `run_jira_story`) | Primary trigger: “work on SCRUM-1” / “work on jira story” |
| **REST helpers** ([`src/integrations/jira/`](../../src/integrations/jira/)) | Get issue, JQL search, comments, lifecycle labels (source of truth) |
| **CLI** (`jira_runner`) | Debug only: `--check-auth`, `--issue`, `--poll-once` |
| **Atlassian MCP** | Optional Studio tooling (`config/mcp_servers.json` → `atlassian`, disabled by default) |

---

## Setup (step by step)

### 1. Jira Cloud site and bot account

1. Use a **Jira Cloud** site (e.g. `https://your-company.atlassian.net`).
2. Prefer a dedicated **bot user** (or your own account for local testing) that can:
   - Browse the project (e.g. `SCRUM`)
   - View issues
   - Add comments
   - Edit labels
3. Confirm you can open issues in the browser while logged in as that user  
   (e.g. `https://your-company.atlassian.net/browse/SCRUM-1`).

### 2. Create an API token (recommended: classic / no scopes)

NFE’s REST client uses **HTTP Basic auth** (`email` + `API token`) against:

```text
https://YOUR-SITE.atlassian.net/rest/api/3/...
```

**Recommended for this project today:** create an API token **without scopes** (classic), which works with that site URL.

1. Open [Manage API tokens](https://id.atlassian.com/manage-profile/security/api-tokens) while logged in as **`JIRA_EMAIL`**.
2. **Create API token**.
3. If the UI offers scopes:
   - **Easiest:** create a token **without scopes** (full account API access, limited by the user’s project permissions).
   - **Or** use **classic scopes** listed in [Scopes](#api-token-scopes) below.
4. Copy the token once; store it only in `.env` (never in Jira comments or git).

> **Scoped granular tokens** (e.g. `read:issue:jira`) require a different base URL  
> (`https://api.atlassian.com/ex/jira/{cloudId}/...`). The current NFE client expects  
> `https://YOUR-SITE.atlassian.net`. Prefer classic/unscoped tokens until gateway URL support is added.

### 3. Project permissions the bot needs

In **Project settings → People / Permissions**, grant the bot (or its role) at least:

| Permission | Why NFE needs it |
|------------|------------------|
| Browse projects | See the project and issues |
| View issues | `GET /rest/api/3/issue/{key}`, JQL search |
| Add comments | Post queued / blocked / result comments |
| Edit issues | Add/remove lifecycle labels (`nfe-queued`, `nfe-running`, …) |

Optional: assign the bot to the project’s default role (e.g. Member) if that role already includes the above.

### 4. Configure `.env`

Copy from [`.env.example`](../../.env.example) and fill:

```ini
# Jira Cloud site (no trailing path)
JIRA_BASE_URL=https://your-site.atlassian.net

# Same Atlassian account that created the API token
JIRA_EMAIL=bot@example.com
JIRA_API_TOKEN=your_api_token_here

# Routing label (default)
NFE_JIRA_LABEL=nfe-agent

# Comma-separated issue types (Story, Task, Bug, Issue, …). Empty or * = any.
NFE_JIRA_ISSUE_TYPES=Story,Task,Bug,Issue

# Only pick up these board statuses (comma-separated)
NFE_JIRA_STATUSES=To Do,In Progress
NFE_JIRA_IN_PROGRESS_STATUS=In Progress

# Optional: custom field id for Acceptance Criteria (otherwise use description)
# NFE_JIRA_ACCEPTANCE_FIELD=customfield_10000

# App login credentials come from Watch-me recording and/or story YAML
# credentials: { username, password } — not global NFE_USER/NFE_PASS
```

### 5. Verify auth

Always use the project venv:

```bash
.venv/bin/python -m src.integrations.jira_runner --check-auth
```

Success looks like: `Jira auth OK as <display name> (...)`.  
If you see **401** on `/myself`, recreate the token for the correct email and update `.env`.

### 6. Labels

Create (or allow auto-create of) these labels:

| Label | Who sets it | Meaning |
|-------|-------------|---------|
| `nfe-agent` | Human | **Required** — route issue to NFE |
| `nfe-queued` / `nfe-running` | Worker | In progress (dedupe) |
| `nfe-recording-ready` | Human | Recording exists; resume after blocked |
| `nfe-blocked` | Worker | Missing recording / bad URL / parse error |
| `nfe-done` | Worker | Finished |

### 7. Create a story for NFE

1. Create a Story/Task in the project.
2. Add label **`nfe-agent`**.
3. If a Watch-me file already exists under `artifacts/recordings/<domain>/` (or legacy flat `artifacts/recordings/`), also add **`nfe-recording-ready`**.
4. Put an NFE config block in the **description** (fenced `yaml` or `json`):

````markdown
## Goal
Run NFE smoke/load test for this journey.

## NFE config

```yaml
target_url: https://opensource-demo.orangehrmlive.com/web/index.php/auth/login
recording: Create Claim
workload:
  executor: shared-iterations
  vus: 10
  iterations: 20
  maxDuration: 5m
thresholds:
  http_req_duration: ["p(95)<2000"]
  http_req_failed: ["rate<0.01"]
credentials:
  username: Admin
  password: admin123
```
````

`target_url` host becomes the **app folder** (`opensource-demo.orangehrmlive.com`). `recording` is the **flow** stem (e.g. `Create Claim` → `artifacts/recordings/opensource-demo.orangehrmlive.com/create-claim.json`). Legacy flat `artifacts/recordings/Create Claim.json` still resolves.

**Credentials (preferred order):** Watch-me recording store (when `NFE_STORE_CREDENTIALS`) → story `credentials:` block → optional legacy `credential_env:` that names env vars to resolve at runtime. Do **not** put passwords in free-text acceptance criteria.

When the story includes a **workload** block, that model is **authoritative** for the k6 run (VUs, iterations, duration/stages, executor, thresholds). The Jira pipeline skips analyse’s default 1 VU × 2 smoke and emits + runs a single script with the story options. If no workload is specified, NFE keeps default smoke and labels `workload_source=default_smoke`.

Jira comments and Confluence run pages report **planned/actual VUs**, **TPS / HTTP req rate**, and the workload source.

### 8. Trigger from Studio chat (primary)

In LangGraph Studio, send one of:

```text
work on SCRUM-1
work on jira story
run jira
process jira issue
```

**Issue selection:**

1. If the message contains a key (`PROJECT-123`), that issue is used.
2. Otherwise NFE lists open issues in **To Do** / **In Progress** with label `nfe-agent`.
3. If **multiple** match, the agent asks which key to work on.
4. If exactly one matches, it is selected automatically.
5. **To Do** → transitioned to **In Progress**, then processed.
6. **In Progress** → NFE reads comments; if they look like prior NFE work it continues, otherwise it asks for clarity.

Add **force** or **re-run** in the message to reprocess or confirm after a clarity ask.

The agent replies in chat with a short summary. Jira gets a **Test Report** comment as **Atlassian Document Format (ADF)** headings/lists (Jira Cloud REST v3 does not render Markdown/wiki markup in API comments).

When smoke/SLA fails or the run aborts mid-way, the comment leads with **Why it failed / stopped** (failed URLs, checks, thresholds, exit code). If Confluence publishing succeeded for a *completed* run, the comment also includes the Confluence run-page URL. See [`confluence-publishing.md`](confluence-publishing.md). Worker design: [`jira-story-worker.md`](jira-story-worker.md).

### 9. Optional CLI (debug)

```bash
# Process one issue by KEY (not the label)
.venv/bin/python -m src.integrations.jira_runner --issue SCRUM-1

# Poll all open nfe-agent issues
.venv/bin/python -m src.integrations.jira_runner --poll-once

# Re-run even if nfe-done / nfe-running
.venv/bin/python -m src.integrations.jira_runner --issue SCRUM-1 --force
```

`--issue` must be the issue key from Jira (e.g. `SCRUM-1`), **not** `nfe-agent`.

---

## API token scopes

### What NFE calls (REST)

| API | Purpose |
|-----|---------|
| `GET /rest/api/3/myself` | Auth check (`--check-auth`) |
| `GET /rest/api/3/issue/{key}` | Read summary, description, labels |
| `POST /rest/api/3/search/jql` | Latest / poll `nfe-agent` issues |
| `POST /rest/api/3/issue/{key}/comment` | Progress + result comments |
| `PUT /rest/api/3/issue/{key}` | Add/remove lifecycle labels |

### Recommended: classic / unscoped token

| Choice | Notes |
|--------|--------|
| **API token without scopes** | Works with `JIRA_BASE_URL=https://site.atlassian.net` + Basic auth. Access is still limited by **project permissions** of `JIRA_EMAIL`. **Preferred for NFE today.** |

### If you must use classic scopes (OAuth / scoped-token UI)

Select at least:

| Scope | Access |
|-------|--------|
| `read:jira-user` | Verify identity (`/myself`) |
| `read:jira-work` | Read issues, search JQL, view labels/fields |
| `write:jira-work` | Add comments, update issue labels |

These map to “View user profiles”, “View Jira issue data”, and “Create and manage issues” on Atlassian’s consent / token UI.

Reference: [Jira scopes for OAuth 2.0 (3LO) and Forge](https://developer.atlassian.com/cloud/jira/platform/scopes-for-oauth-2-3LO-and-forge-apps/).

### Granular scopes (only if using API gateway)

If you create a **scoped granular** token, Atlassian expects:

```text
https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3/...
```

Minimum granular scopes for NFE’s operations:

| Scope | Used for |
|-------|----------|
| `read:jira-user` or `read:me` / user profile equivalent | Auth check |
| `read:issue:jira` | Get issue |
| `read:issue-details:jira` | Description / fields (when required by API) |
| `read:project:jira` | Project context / browse |
| `write:issue:jira` | Update labels on the issue |
| `write:comment:jira` | Post comments |
| JQL / search scopes as listed on the [search API](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/) docs | chat latest-issue / `--poll-once` |

**NFE does not set `JIRA_BASE_URL` to the gateway by default.** Stick to an **unscoped (classic) token** unless you intentionally configure the gateway URL and cloud id.

### Scopes NFE does **not** need

- Create/delete issues (`delete:issue:jira`)
- Admin / project admin scopes
- Confluence, Bitbucket, or Compass scopes
- Attachment upload (results are commented as paths, not uploaded files)

### Atlassian MCP (optional Studio)

MCP uses Atlassian’s remote MCP / OAuth flow (see [Atlassian Rovo MCP](https://www.atlassian.com/platform/rovo-mcp)). Enable only for interactive Studio use:

1. Set `"enabled": true` on `atlassian` in [`config/mcp_servers.json`](../../config/mcp_servers.json).
2. Complete OAuth when prompted.
3. Keep REST env vars for the chat-driven pipeline — MCP is not used for processing stories.

---

## Recording gate

If `artifacts/recordings/<domain>/<flow>.json` is missing (legacy flat `artifacts/recordings/<name>.json` also checked), the worker comments instructions, sets `nfe-blocked`, and stops. After Watch-me, add **`nfe-recording-ready`** (keep `nfe-agent`) and say **work on SCRUM-1** again (or **force** if needed).

---

## Security notes

- Never put app passwords in free-text AC; use Watch-me recording store and/or story `credentials:` (optional legacy `credential_env:` for env indirection).
- Comments are sanitized ([`src/integrations/jira/security.py`](../../src/integrations/jira/security.py)).
- Parsed `target_url` goes through NFE URL policy.
- Chat replies mirror Jira comments briefly and do not dump secrets.
- See also [`security.md`](../security/security.md).

---

## Troubleshooting

### `HTTP 401` on `--check-auth` (`GET /myself`)

- Token was created under a **different** Atlassian account than `JIRA_EMAIL`.
- Token revoked/expired — create a new one.
- Using a **scoped granular** token against `https://site.atlassian.net` (use classic/unscoped, or switch to gateway URL).

### `HTTP 404` on `GET /issue/SCRUM-1`

Jira often returns **404** (not 403) when the token account cannot see the issue, or auth failed:

1. Open the issue in the browser as `JIRA_EMAIL`.
2. Run `--check-auth` until it succeeds.
3. Confirm `JIRA_BASE_URL` matches the site in the browser bar.
4. Ensure the bot has **Browse / View issues** on that project.

### `No module named 'requests'` / wrong Python

Homebrew `python` may be aliased past the venv. Always:

```bash
.venv/bin/python -m src.integrations.jira_runner --check-auth
```

### Chat says no open `nfe-agent` issues

1. On the issue, confirm the label is exactly **`nfe-agent`** (no trailing comma — `nfe-agent,` will not match JQL).
2. Confirm status is **To Do** or **In Progress** (`NFE_JIRA_STATUSES`).
3. Confirm the issue type is listed in `NFE_JIRA_ISSUE_TYPES` (default: `Story,Task,Bug,Issue`).
4. Or include the key: **work on SCRUM-1**.
