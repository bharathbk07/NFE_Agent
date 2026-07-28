"""Load environment-backed application and model-routing settings."""

import os
import json
from dotenv import load_dotenv

load_dotenv()


def _parse_model_list() -> list[str]:
    """Build model references from environment configuration.

    Returns:
        Ordered provider/model references available to the model router.
    """
    models_env = os.getenv("LLM_MODELS", "").strip()
    if models_env:
        return [m.strip() for m in models_env.split(",") if m.strip()]
    single = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    models: list[str] = [single] if single else ["gemini-2.5-flash"]

    # Auto-include default Cursor model when SDK credentials are configured
    cursor_key = os.getenv("CURSOR_API_KEY", "").strip()
    cursor_runtime = os.getenv("CURSOR_RUNTIME", "local").strip().lower()
    cursor_repo = os.getenv("CURSOR_CLOUD_REPO", "").strip()
    cursor_model = os.getenv("CURSOR_DEFAULT_MODEL", "composer-2.5").strip()
    cursor_sdk_ready = cursor_key and (
        cursor_runtime != "cloud" or bool(cursor_repo)
    )
    if cursor_sdk_ready and not models_env:
        cursor_ref = f"cursor:{cursor_model}"
        if cursor_ref not in models and cursor_model not in models:
            models.append(cursor_ref)

    return models


def _parse_task_routing() -> dict[str, str]:
    """Parse optional task-to-model routing JSON.

    Returns:
        A task-name to model-reference mapping, or an empty mapping when absent
        or invalid.
    """
    raw = os.getenv("LLM_TASK_ROUTING", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


class Settings:
    """Expose immutable-at-import environment configuration.

    Attributes:
        GEMINI_API_KEY: Credential used by the Gemini provider.
        LLM_MODELS: Comma-separated model references for automatic routing.
        DEBUG_MODE: Whether verbose execution and visible browser mode are enabled.
    """
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Multi-model: comma-separated provider-prefixed refs, e.g.
    # google:gemini-3.1-flash-lite,google:gemini-3.5-flash,cursor:composer-2.5
    LLM_MODELS: str = os.getenv("LLM_MODELS", "")
    LLM_TASK_ROUTING: str = os.getenv("LLM_TASK_ROUTING", "")

    # Cursor AI (native cursor-sdk — see README)
    CURSOR_API_KEY: str = os.getenv("CURSOR_API_KEY", "")
    CURSOR_RUNTIME: str = os.getenv("CURSOR_RUNTIME", "local")  # local | cloud
    CURSOR_CLOUD_REPO: str = os.getenv("CURSOR_CLOUD_REPO", "")
    CURSOR_WORKDIR: str = os.getenv("CURSOR_WORKDIR", "")
    CURSOR_DEFAULT_MODEL: str = os.getenv("CURSOR_DEFAULT_MODEL", "composer-2.5")

    # LangSmith
    LANGCHAIN_TRACING_V2: bool = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    LANGCHAIN_API_KEY: str = os.getenv("LANGCHAIN_API_KEY", "")
    LANGCHAIN_PROJECT: str = os.getenv("LANGCHAIN_PROJECT", "mcp-agent-service")

    # Execution
    DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"

    # Security policy (see docs/security/security.md)
    # Comma-separated host allowlist; empty = any non-blocked host
    NFE_URL_ALLOWLIST: str = os.getenv("NFE_URL_ALLOWLIST", "")
    NFE_URL_DENY_PRIVATE: bool = (
        os.getenv("NFE_URL_DENY_PRIVATE", "true").lower() == "true"
    )
    NFE_ALLOW_LOCALHOST: bool = (
        os.getenv("NFE_ALLOW_LOCALHOST", "false").lower() == "true"
    )
    NFE_REDACT_ARTIFACTS: bool = (
        os.getenv("NFE_REDACT_ARTIFACTS", "true").lower() == "true"
    )
    # Persist app credentials into recordings / IR / k6 scripts (per-app from Watch-Me).
    # Default true: multi-app demos cannot rely on a single NFE_USER/NFE_PASS env pair.
    # Still redact passwords in Jira/Confluence comments via redact_text_for_llm.
    NFE_STORE_CREDENTIALS: bool = (
        os.getenv("NFE_STORE_CREDENTIALS", "true").lower() == "true"
    )
    NFE_SELF_HEAL_HTML_CHARS: int = int(
        os.getenv("NFE_SELF_HEAL_HTML_CHARS", "15000") or "15000"
    )
    NFE_SELF_HEAL_A11Y_CHARS: int = int(
        os.getenv("NFE_SELF_HEAL_A11Y_CHARS", "6000") or "6000"
    )

    # Jira Cloud integration (chat-driven — see docs/workers/jira-integration.md)
    JIRA_BASE_URL: str = os.getenv("JIRA_BASE_URL", "").rstrip("/")
    JIRA_EMAIL: str = os.getenv("JIRA_EMAIL", "")
    JIRA_API_TOKEN: str = os.getenv("JIRA_API_TOKEN", "")
    NFE_JIRA_LABEL: str = os.getenv("NFE_JIRA_LABEL", "nfe-agent")
    # Empty = auto-build from NFE_JIRA_LABEL + NFE_JIRA_ISSUE_TYPES
    NFE_JIRA_POLL_JQL: str = os.getenv("NFE_JIRA_POLL_JQL", "")
    # Comma-separated issue types; empty or * = any type
    NFE_JIRA_ISSUE_TYPES: str = os.getenv(
        "NFE_JIRA_ISSUE_TYPES", "Story,Task,Bug,Issue"
    )
    # Comma-separated board statuses eligible for pickup
    NFE_JIRA_STATUSES: str = os.getenv(
        "NFE_JIRA_STATUSES", "To Do,In Progress"
    )
    # Status name to move into when starting work from To Do
    NFE_JIRA_IN_PROGRESS_STATUS: str = os.getenv(
        "NFE_JIRA_IN_PROGRESS_STATUS", "In Progress"
    )
    NFE_JIRA_ACCEPTANCE_FIELD: str = os.getenv(
        "NFE_JIRA_ACCEPTANCE_FIELD", ""
    )  # optional custom field id e.g. customfield_10000

    # Confluence Cloud publishing (see docs/workers/confluence-publishing.md)
    # Empty BASE/EMAIL/TOKEN fall back to JIRA_* values.
    CONFLUENCE_BASE_URL: str = os.getenv("CONFLUENCE_BASE_URL", "").rstrip("/")
    CONFLUENCE_EMAIL: str = os.getenv("CONFLUENCE_EMAIL", "")
    CONFLUENCE_API_TOKEN: str = os.getenv("CONFLUENCE_API_TOKEN", "")
    CONFLUENCE_SPACE_KEY: str = os.getenv("CONFLUENCE_SPACE_KEY", "")
    CONFLUENCE_PARENT_TITLE: str = os.getenv(
        "CONFLUENCE_PARENT_TITLE", "Performance Testing and Engineering"
    )
    NFE_CONFLUENCE_PUBLISH: bool = os.getenv(
        "NFE_CONFLUENCE_PUBLISH", "true"
    ).lower() in ("1", "true", "yes", "on")

    # k6 threshold watcher: abort early on *catastrophic* breaches (see docs)
    NFE_K6_ABORT_ON_FAIL: bool = os.getenv(
        "NFE_K6_ABORT_ON_FAIL", "true"
    ).lower() in ("1", "true", "yes", "on")
    NFE_K6_ABORT_DELAY: str = os.getenv("NFE_K6_ABORT_DELAY", "10s") or "10s"
    # Stop when HTTP fail rate reaches this fraction (0.60 = 60% of requests failed)
    NFE_K6_ABORT_FAIL_RATE: float = float(
        os.getenv("NFE_K6_ABORT_FAIL_RATE", "0.60") or "0.60"
    )
    # Stop when p99 latency exceeds this many ms (0 = disable)
    NFE_K6_ABORT_P99_MS: int = int(os.getenv("NFE_K6_ABORT_P99_MS", "30000") or "30000")
    # Stop when checks pass-rate falls below this (0 = disable; 0.40 = <40% pass)
    NFE_K6_ABORT_CHECKS_MIN: float = float(
        os.getenv("NFE_K6_ABORT_CHECKS_MIN", "0.40") or "0.40"
    )
    # If true, also abortOnFail on tight SLA thresholds (1% errors, p95, …).
    # Default false: SLA fails the test at end; only catastrophe aborts mid-run.
    NFE_K6_SLA_ABORT_ON_FAIL: bool = os.getenv(
        "NFE_K6_SLA_ABORT_ON_FAIL", "false"
    ).lower() in ("1", "true", "yes", "on")

    # App-scoped artifacts + local ChromaDB RAG (see docs/pipeline/app-artifacts-and-knowledge.md)
    # Fallback app id only when no target URL exists yet (rare bootstrap).
    NFE_DEFAULT_APP: str = os.getenv("NFE_DEFAULT_APP", "").strip()
    NFE_RAG_ENABLED: bool = os.getenv("NFE_RAG_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    NFE_RAG_TOP_K: int = int(os.getenv("NFE_RAG_TOP_K", "4") or "4")

    # Project MCP registry (single file for all MCP server definitions)
    # Default: <repo>/config/mcp_servers.json
    MCP_SERVERS_CONFIG: str = os.getenv("MCP_SERVERS_CONFIG", "")

    @property
    def available_models(self) -> list[str]:
        """Return model references currently available for routing.

        Returns:
            Ordered provider/model references.
        """
        return _parse_model_list()

    @property
    def llm_task_routing(self) -> dict[str, str]:
        """Return explicit task-to-model routing overrides.

        Returns:
            A task-name to model-reference mapping.
        """
        return _parse_task_routing()

    @property
    def mcp_servers_config_path(self) -> str:
        """Return the configured or default MCP registry path.

        Returns:
            MCP registry path as a string.
        """
        if self.MCP_SERVERS_CONFIG.strip():
            return self.MCP_SERVERS_CONFIG.strip()
        from pathlib import Path

        return str(Path(__file__).resolve().parent / "mcp_servers.json")


settings = Settings()
