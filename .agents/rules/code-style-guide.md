---
trigger: always_on
---

AI Agent Engineering Standards & "Antigravity" Blueprint

Production-Grade Standards for LangChain, LangGraph, LangSmith, and Dynatrace Observability

This document establishes the architecture, directory standards, error handling policies, and observability configurations for our Python-based AI Agent ecosystem. Adherence to these guidelines is mandatory for ensuring system resilience, ease of maintenance, and high observability.

📂 1. Standardized Directory & Folder Structure

To keep the project organized and modular as we scale from simple LangChain setups to stateful LangGraph topologies, developers must strictly adhere to the following layout.

my-mcp-agent/
│
├── .env                          # Local secret environmental variables (git-ignored)
├── .env.example                  # Template with non-secret variable definitions
├── .gitignore                    # Standard Python, IDE, and .env ignores
├── requirements.txt              # Primary project dependencies
├── README.md                     # Setup instructions and architectural overview
│
├── config/                       # Application configuration and environment managers
│   ├── __init__.py
│   ├── settings.py               # Pydantic-based configuration model
│   └── observability.py          # Dynatrace, OpenTelemetry, and LangSmith setup engines
│
├── src/                          # Application source directory
│   ├── __init__.py
│   │
│   ├── agents/                   # Core LLM reasoning blocks and state structures
│   │   ├── __init__.py
│   │   ├── state.py              # LangGraph state schemas (TypedDict / Pydantic models)
│   │   ├── browser_agent.py      # Main web automation agent logic
│   │   └── base_agent.py         # Abstract base agent configurations
│   │
│   ├── tools/                    # Custom agent toolkits & MCP client wrappers
│   │   ├── __init__.py
│   │   ├── playwright_mcp.py     # Playwright MCP integration interface
│   │   ├── parser.py             # Data scrapers & extractors (BeautifulSoup fallbacks)
│   │   └── system_tools.py       # Basic OS, API, and math utilities
│   │
│   └── utils/                    # Reusable helper utilities
│       ├── __init__.py
│       ├── formatting.py         # String processing and output styling
│       └── validation.py         # Input parsing and payload sanity checks
│
└── tests/                        # Execution checks and integration suites
    ├── __init__.py
    ├── conftest.py               # Shared pytest fixtures
    ├── test_mcp_connection.py    # MCP diagnostic assertions
    └── test_observability.py     # Log propagation and trace tests


🛡️ 2. Resilient Error Handling & Graceful Degradation

AI Agent execution is non-deterministic. External tool invocations (such as Playwright running browser operations), API networks, and LLM providers will fail. Developers must implement strict safety boundaries:

Rule A: The "Never Panic" Standard

Any external API request or OS tool invocation must be safely wrapped in exception blocks. Never allow a failure in an optional tool execution (like screenshot generation or site scraping) to terminate the agent loop completely. Always fall back gracefully.

Rule B: Exponential API Retries

Instantiate LLMs and external networks with built-in retry logic.

Use standard backoffs for rates, transient 500s, and connection timeouts.

Rule C: Structured Tool Failures

Custom tools should never raise unhandled exceptions to the parent execution chain. Instead, catch exceptions, format them as structured errors, and return them as string payloads to the LLM so it can attempt self-healing (e.g., trying a different CSS selector or a simpler page-fetch technique).

🐍 3. Reusable Boilerplates & Coding Patterns

A. State Definitions & Type Verification (src/agents/state.py)

"""
Module: State Definition
Description: Defines the shared state schema for LangGraph routing.
"""
from typing import Annotated, Sequence, TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    """
    Type-safe core schema holding active states across state nodes.
    Uses 'add_messages' annotation to enable seamless message appending.
    """
    messages: Annotated[Sequence[BaseMessage], add_messages]
    current_url: str
    retry_count: int
    error_log: list[str]


B. Graceful Tool Design Boilerplate (src/tools/playwright_mcp.py)

"""
Module: Playwright Scraper Tool with Graceful Degradation
Description: Interacts with pages. Integrates lightweight requests fallback.
"""
import logging
from langchain_core.tools import tool
import requests

logger = logging.getLogger(__name__)

@tool
def fetch_page_content(url: str) -> str:
    """
    Extracts text content from any public webpage using browser rendering.
    
    Args:
        url (str): The target website address to extract text from.
        
    Returns:
        str: Extracted webpage text content or error diagnostic logs.
    """
    # Guardrail: Quick validation
    if not url.startswith(("http://", "https://")):
        return f"Error: Invalid schema provided for URL '{url}'. Please include HTTP/HTTPS protocols."

    try:
        # PRIMARY PATH: Try executing via high-fidelity headless browser rendering
        return _run_playwright_extraction(url)
    except Exception as browser_error:
        logger.warning("Primary browser scraping failed. Falling back to simple HTTP request. Details: %s", browser_error)
        
        # DEGRADED FALLBACK PATH: Fall back to non-JS rendering requests immediately
        try:
            return _run_static_http_fallback(url)
        except Exception as fallback_error:
            logger.critical("Scraping fallback failed entirely on URL: %s", url)
            return (
                f"Scraping Engine Error: Both high-fidelity and static scraping failbacks "
                f"unsuccessfully targeted the host. Details: {str(fallback_error)}"
            )

def _run_playwright_extraction(url: str) -> str:
    """Helper representing the primary execution code (e.g., Playwright extraction logic)."""
    # Simulated execution - replace with real browser driver calls
    raise RuntimeError("Headless browser environment failed to load.")

def _run_static_http_fallback(url: str) -> str:
    """Helper representing static backup scraper."""
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    # Simple sanitization
    return response.text[:2000]  # Return structured slice to keep tokens clean


📈 4. Granular Observability (LangSmith & Dynatrace)

Every agent action, tool invocation, token count, and exception must be captured via dual-layer tracing.

                  ┌───────────────────────────────────────────────┐
                  │              LangChain Application            │
                  └───────────────────────┬───────────────────────┘
                                          │
                         ┌────────────────┴────────────────┐
                         ▼                                 ▼
            ┌────────────────────────┐         ┌───────────────────────┐
            │       LangSmith        │         │      Dynatrace        │
            │   (Prompt debugging,   │         │  (Unified enterprise  │
            │  latency tracing, run  │         │   APM logs, overall   │
            │   outputs, cost metric)│         │  infrastructure view) │
            └────────────────────────┘         └───────────────────────┘


LangSmith Optimization Guidelines

Custom Run Names: Never rely on default ChatGoogleGenerativeAI tag identifiers in production. Declare explicit run_name properties on all chain invocations.

Context-Aware Metadata: Embed structural tags (e.g., user_id, session_id, environment) inside model context parameters.

Prompt Versioning: Link systems to the LangSmith prompt registry rather than keeping inline code templates.

Dynatrace & OpenTelemetry Configuration (config/observability.py)

"""
Module: Dual Observability System Configurator
Description: Integrates LangSmith tracing configurations alongside standard 
             OpenTelemetry metrics targeting Dynatrace.
"""
import os
import logging
from traceloop.sdk import Traceloop
from langchain_core.callbacks import StdOutCallbackHandler

logger = logging.getLogger(__name__)

def initialize_observability() -> None:
    """
    Initializes telemetries globally. Configures both the local LangChain
    tracing flags and the system's OpenTelemetry routing paths.
    """
    # 1. Verify LangSmith Setup
    ls_tracing = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    if ls_tracing:
        logger.info("LangSmith tracing detected in environment and active.")
    else:
        logger.warning("LangSmith tracing is disabled. Deep agent trace metrics will not be pushed.")

    # 2. Verify Dynatrace/OpenTelemetry (via Traceloop SDK / OpenLLMetry)
    dt_api_url = os.getenv("TRACELOOP_BASE_URL") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    dt_token = os.getenv("TRACELOOP_HEADERS") or os.getenv("OTEL_EXPORTER_OTLP_HEADERS")

    if dt_api_url and dt_token:
        try:
            # Setup OpenLLMetry pipeline directly to Dynatrace OTLP endpoint
            Traceloop.init(
                app_name=os.getenv("LANGCHAIN_PROJECT", "mcp-agent-service"),
                disable_batches=False,
                traceloop_sync_enabled=False
            )
            logger.info("Dynatrace OpenLLMetry integration configured successfully.")
        except Exception as otel_err:
            logger.error("Failed to initialize OpenTelemetry exporter for Dynatrace: %s", otel_err)
    else:
        logger.warning("Dynatrace endpoint variables are incomplete. Dynatrace APM tracing skipped.")

def get_diagnostics_callbacks() -> list:
    """
    Returns standard diagnostic callbacks for fallback execution monitoring.
    """
    callbacks = []
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        callbacks.append(StdOutCallbackHandler())
    return callbacks


🚀 5. Developer Action Checklist for Deployment

When building new workflows or tools, you must pass this checklist before staging code for review:

[ ] Type Validation: State objects and inputs are strictly configured using Python typing constructs.

[ ] Structured Logs: The tool handles errors and does not raise raw stack traces directly into the execution graph loop.

[ ] Trace Integration: config={ "run_name": "<descriptive_name>", "tags": [...] } is declared on every call.

[ ] Performance Validation: All custom tools have a defined timeout configured (e.g. requests.get(..., timeout=5)).

[ ] Environment Sync: Required API key updates are documented in .env.example before checking code into the repository.