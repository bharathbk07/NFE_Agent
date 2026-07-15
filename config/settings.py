import os
import json
from dotenv import load_dotenv

load_dotenv()


def _parse_model_list() -> list[str]:
    """Build the list of available models from env configuration."""
    models_env = os.getenv("LLM_MODELS", "").strip()
    if models_env:
        return [m.strip() for m in models_env.split(",") if m.strip()]
    single = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    return [single] if single else ["gemini-2.5-flash"]


def _parse_task_routing() -> dict[str, str]:
    """Parse optional explicit task→model routing JSON from env."""
    raw = os.getenv("LLM_TASK_ROUTING", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


class Settings:
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    # Multi-model: comma-separated list; falls back to GEMINI_MODEL when unset
    LLM_MODELS: str = os.getenv("LLM_MODELS", "")
    LLM_TASK_ROUTING: str = os.getenv("LLM_TASK_ROUTING", "")

    # LangSmith
    LANGCHAIN_TRACING_V2: bool = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    LANGCHAIN_API_KEY: str = os.getenv("LANGCHAIN_API_KEY", "")
    LANGCHAIN_PROJECT: str = os.getenv("LANGCHAIN_PROJECT", "mcp-agent-service")

    # Execution
    DEBUG_MODE: bool = os.getenv("DEBUG_MODE", "false").lower() == "true"

    # Project MCP registry (single file for all MCP server definitions)
    # Default: <repo>/config/mcp_servers.json
    MCP_SERVERS_CONFIG: str = os.getenv("MCP_SERVERS_CONFIG", "")

    @property
    def available_models(self) -> list[str]:
        return _parse_model_list()

    @property
    def llm_task_routing(self) -> dict[str, str]:
        return _parse_task_routing()

    @property
    def mcp_servers_config_path(self) -> str:
        if self.MCP_SERVERS_CONFIG.strip():
            return self.MCP_SERVERS_CONFIG.strip()
        from pathlib import Path

        return str(Path(__file__).resolve().parent / "mcp_servers.json")


settings = Settings()
