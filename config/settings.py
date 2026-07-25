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

    # Security policy (see docs/security.md)
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
    NFE_STORE_CREDENTIALS: bool = (
        os.getenv("NFE_STORE_CREDENTIALS", "false").lower() == "true"
    )
    NFE_SELF_HEAL_HTML_CHARS: int = int(
        os.getenv("NFE_SELF_HEAL_HTML_CHARS", "15000") or "15000"
    )
    NFE_SELF_HEAL_A11Y_CHARS: int = int(
        os.getenv("NFE_SELF_HEAL_A11Y_CHARS", "6000") or "6000"
    )

    # Jira Cloud integration (chat-driven — see docs/jira-integration.md)
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
