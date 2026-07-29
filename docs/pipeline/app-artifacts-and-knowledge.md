# App-scoped artifacts, knowledge, and local ChromaDB RAG

Organize Watch-me recordings, k6 scripts, and markdown knowledge **by URL domain** (application), then index knowledge in a **local ChromaDB** for Analysis QA and reuse hints.

## App id = URL domain

The application folder name is the **host** from `target_url`:

1. Parse `urlparse(...).netloc`
2. Strip port and a leading `www.`
3. Sanitize filesystem-unsafe characters (`[^a-zA-Z0-9._-]` → `_`)

Example:

`https://opensource-demo.orangehrmlive.com/web/index.php/auth/login`  
→ app folder `opensource-demo.orangehrmlive.com`

**Resolution order:**

1. Domain from `target_url` / recording URL (primary)
2. Else domain from an explicit URL in Jira YAML / chat
3. Else `NFE_DEFAULT_APP` only when no URL exists yet (rare bootstrap)

There is **no** friendly rename map (`orange-hrm`) and **no** `config/apps.yaml`.

**Flow id** = Watch-me label / recording stem (e.g. `create-claim`). If unlabeled, a short path slug or `default`.

## Layout

```text
artifacts/
  k6/<domain>/<flow>.js
  k6/<domain>/<flow>_ir.json
  recordings/<domain>/<flow>.json
  knowledge/<domain>/overview.md
  knowledge/<domain>/flows/<flow>.md
  rag/chroma/                 # Chroma persistent client
```

## Initialization

- `ensure_workspace()` runs once on graph / CLI startup: creates base dirs (`k6`, `recordings`, `knowledge`, `rag/chroma`) and ensures the Chroma collection exists (empty OK).
- `ensure_app_dirs(app_id)` runs **lazily** the first time a domain is used (creates per-app folders + seeds `overview.md`).

## Backward compatibility

App/domain paths are preferred. Readers still fall back to legacy flat files:

- `artifacts/recordings/<host>.json`
- `artifacts/k6/<host>.js` / `<host>_ir.json` (legacy flat; prefer `k6/<domain>/<flow>.*`)

## Markdown knowledge

After a successful analyse / Jira workload smoke, NFE upserts a **flow card** under `knowledge/<domain>/flows/<flow>.md` with target URL, artifact paths, TXN names, workload source, and last smoke status.

## Local ChromaDB RAG

- Persistent path: `artifacts/rag/chroma`
- Collection: `nfe_knowledge`
- Document id: `{app}::{kind}::{flow_or_overview}`
- Embeddings: Chroma default local function (no cloud embedding API)
- Only indexes files under `artifacts/knowledge/` (never raw HAR / credentials)

**Soft-fail:** if `chromadb` is missing, import fails, or embed fails, NFE continues with markdown-only context (same pattern as missing k6).

### Settings

| Env | Default | Purpose |
|-----|---------|---------|
| `NFE_DEFAULT_APP` | _(empty)_ | Fallback app id when no URL exists |
| `NFE_RAG_ENABLED` | `true` | Disable RAG entirely |
| `NFE_RAG_TOP_K` | `4` | Chunks returned per query |

Disable RAG: `NFE_RAG_ENABLED=false`.

## How RAG is used

1. **Analysis QA** — prompt context = compact state summary + direct flow markdown (if known) + RAG top-k chunks for the question (filtered by current app when set). Citations include source paths (“Retrieved from …”).
2. **Reuse / list** — natural-language recording hints can query RAG for `app/flow` suggestions before filename heuristics.

Intent routing does **not** auto-skip Navigator/Orchestrator based on similarity (later follow-up).

## Code map

| Module | Role |
|--------|------|
| [`src/utils/app_registry.py`](../../src/utils/app_registry.py) | Domain/flow ids, `ensure_app_dirs` |
| [`src/utils/workspace.py`](../../src/utils/workspace.py) | `ensure_workspace` |
| [`src/utils/artifacts.py`](../../src/utils/artifacts.py) | App-scoped k6 / IR |
| [`src/utils/recording_store.py`](../../src/utils/recording_store.py) | App-scoped recordings + legacy fallback |
| [`src/utils/knowledge_store.py`](../../src/utils/knowledge_store.py) | Markdown overview / flow cards |
| [`src/utils/rag_store.py`](../../src/utils/rag_store.py) | Chroma upsert / query |

## Jira

App folder comes from the story `target_url` host. `recording:` is the **flow** stem (e.g. `Create Claim` → `create-claim` under that domain). Legacy flat recording names still resolve.
