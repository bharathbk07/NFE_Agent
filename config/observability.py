"""Configure LangSmith, OpenTelemetry, and Dynatrace observability."""
import logging
import os
import queue
import threading
import time
from typing import Any
from urllib.parse import unquote

import requests
from langchain_core.callbacks import StdOutCallbackHandler

logger = logging.getLogger(__name__)

_INITIALIZED = False


def _normalize_dynatrace_env_url(base_url: str) -> str:
    """Remove an OTLP suffix from a Dynatrace URL.

    Args:
        base_url: Dynatrace environment or OTLP endpoint URL.

    Returns:
        The environment root URL without a trailing slash.
    """
    url = base_url.rstrip("/")
    for suffix in ("/api/v2/otlp", "/api/v2"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    return url


def _normalize_otlp_base_url(base_url: str) -> str:
    """Normalize a Dynatrace URL to its OTLP base endpoint.

    Args:
        base_url: Dynatrace environment, API v2, or OTLP endpoint URL.

    Returns:
        A URL ending in ``/api/v2/otlp``.
    """
    url = base_url.rstrip("/")
    if url.endswith("/api/v2/otlp"):
        return url
    if url.endswith("/api/v2"):
        return f"{url}/otlp"
    return f"{url}/api/v2/otlp"


def _parse_dynatrace_headers(raw_headers: str) -> tuple[dict[str, str], str]:
    """Parse supported Dynatrace authorization header formats.

    Args:
        raw_headers: Encoded OTLP headers, an Authorization header, or a token.

    Returns:
        A tuple of exporter headers and the extracted API token.
    """
    if not raw_headers:
        return {}, ""

    headers: dict[str, str] = {}
    token = ""

    if "Authorization=" in raw_headers or "authorization=" in raw_headers.lower():
        for part in raw_headers.split(","):
            part = part.strip()
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = unquote(value.strip())
            headers[key] = value
            if value.lower().startswith("api-token "):
                token = value.split(" ", 1)[1].strip()
    elif raw_headers.lower().startswith("authorization:"):
        value = raw_headers.split(":", 1)[1].strip()
        headers["Authorization"] = value
        if value.lower().startswith("api-token "):
            token = value.split(" ", 1)[1].strip()
    elif "Api-Token=" in raw_headers:
        token = raw_headers.split("Api-Token=", 1)[1].strip().strip('"').strip("'")
        headers["Authorization"] = f"Api-Token {token}"
    else:
        token = raw_headers.strip().strip('"').strip("'")
        headers["Authorization"] = f"Api-Token {token}"

    return headers, token


def _trace_context_from_record(record: logging.LogRecord) -> tuple[str | None, str | None]:
    """Resolve trace identifiers from a record or active OpenTelemetry span.

    Args:
        record: Standard-library log record.

    Returns:
        A ``(trace_id, span_id)`` tuple; each value is ``None`` when unavailable.
    """
    trace_id = getattr(record, "otelTraceID", None)
    span_id = getattr(record, "otelSpanID", None)
    if trace_id and trace_id != "0":
        return str(trace_id).lower(), str(span_id or "0").lower()

    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except Exception:
        pass

    return None, None


def _enrich_log_content(content: str, trace_id: str | None, span_id: str | None) -> str:
    """Add a Dynatrace correlation marker when trace context exists.

    Args:
        content: Formatted log message.
        trace_id: Optional hexadecimal trace identifier.
        span_id: Optional hexadecimal span identifier.

    Returns:
        Correlated or unchanged log content.
    """
    if trace_id and span_id:
        return f"[!dt dt.trace_id={trace_id},dt.span_id={span_id}] - {content}"
    return content


class DynatraceLogHandler(logging.Handler):
    """Queue correlated records for Dynatrace Log Ingestion API v2."""

    def __init__(self, env_url: str, api_token: str, app_name: str = "nfe-agent"):
        """Initialize the asynchronous ingestion handler.

        Args:
            env_url: Dynatrace environment root URL.
            api_token: API token authorized for log ingestion.
            app_name: Service name attached to emitted records.
        """
        super().__init__()
        self.url = f"{env_url.rstrip('/')}/api/v2/logs/ingest"
        self.headers = {
            "Authorization": f"Api-Token {api_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        self.app_name = app_name
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self.worker = threading.Thread(target=self._post_logs_worker, daemon=True)
        self.worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Queue a formatted log record for ingestion.

        Args:
            record: Standard-library log record to serialize.
        """
        try:
            trace_id, span_id = _trace_context_from_record(record)
            content = _enrich_log_content(self.format(record), trace_id, span_id)

            log_entry: dict[str, Any] = {
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(record.created)
                ),
                "content": content,
                "severity": record.levelname,
                "log.source": record.name,
                "service.name": self.app_name,
            }
            if trace_id and span_id:
                log_entry["dt.trace_id"] = trace_id
                log_entry["dt.span_id"] = span_id
                log_entry["trace_id"] = trace_id
                log_entry["span_id"] = span_id

            self.queue.put(log_entry)
        except Exception:
            self.handleError(record)

    def _post_logs_worker(self) -> None:
        """Continuously post queued records in batches.

        Returns:
            This daemon worker does not return during normal operation.
        """
        while True:
            batch: list[dict[str, Any]] = []
            try:
                item = self.queue.get(timeout=2.0)
                batch.append(item)
                while len(batch) < 100:
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                continue

            if batch:
                try:
                    response = requests.post(
                        self.url, json=batch, headers=self.headers, timeout=5
                    )
                    response.raise_for_status()
                except Exception:
                    pass


def _configure_otlp_log_export(
    otlp_base_url: str,
    otlp_headers: dict[str, str],
    service_name: str,
) -> None:
    """Install a root handler that exports Python logs over OTLP.

    Args:
        otlp_base_url: OTLP base endpoint without the signal-specific suffix.
        otlp_headers: Authentication headers for the exporter.
        service_name: OpenTelemetry service resource name.

    Raises:
        ImportError: If required OpenTelemetry logging packages are unavailable.
        Exception: If provider or exporter setup fails.
    """
    from opentelemetry._logs import set_logger_provider
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": service_name})
    logger_provider = LoggerProvider(resource=resource)
    exporter = OTLPLogExporter(
        endpoint=f"{otlp_base_url.rstrip('/')}/v1/logs",
        headers=otlp_headers or None,
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(logger_provider)

    otlp_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger().addHandler(otlp_handler)
    logger.info("OpenTelemetry OTLP log export configured for distributed tracing.")


def _configure_log_correlation() -> None:
    """Inject active trace context into standard-library log records.

    Raises:
        ImportError: If OpenTelemetry logging instrumentation is unavailable.
        Exception: If instrumentation cannot be installed.
    """
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    LoggingInstrumentor().instrument(set_logging_format=False)
    logger.info("OpenTelemetry log correlation instrumentation enabled.")


def initialize_observability() -> None:
    """Initialize process-wide tracing and log export once.

    Returns:
        ``None``. Missing optional dependencies or incomplete configuration are
        logged and skipped.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    _INITIALIZED = True

    ls_tracing = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
    if ls_tracing:
        logger.info("LangSmith tracing detected in environment and active.")
    else:
        logger.warning(
            "LangSmith tracing is disabled. Deep agent trace metrics will not be pushed."
        )

    dt_api_url = os.getenv("TRACELOOP_BASE_URL") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    raw_headers = os.getenv("TRACELOOP_HEADERS") or os.getenv("OTEL_EXPORTER_OTLP_HEADERS", "")
    otlp_headers, dt_token_val = _parse_dynatrace_headers(raw_headers)

    if not dt_api_url or not dt_token_val:
        logger.warning(
            "Dynatrace endpoint variables are incomplete. Dynatrace APM tracing skipped."
        )
        return

    otlp_base_url = _normalize_otlp_base_url(dt_api_url)
    env_url = _normalize_dynatrace_env_url(dt_api_url)
    service_name = os.getenv("LANGCHAIN_PROJECT", "mcp-agent-service")

    os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", otlp_base_url)
    os.environ.setdefault("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
    if otlp_headers.get("Authorization"):
        os.environ.setdefault(
            "OTEL_EXPORTER_OTLP_HEADERS",
            f"Authorization={otlp_headers['Authorization']}",
        )

    # Initialize traces first so later log handlers can correlate active spans.
    try:
        from traceloop.sdk import Traceloop

        Traceloop.init(
            app_name=service_name,
            disable_batch=False,
            traceloop_sync_enabled=False,
        )
        logger.info("Dynatrace OpenLLMetry tracing configured successfully.")
    except ImportError:
        logger.warning(
            "traceloop-sdk is not installed. Skipping Dynatrace OpenLLMetry initialization."
        )
    except Exception as otel_err:
        logger.error("Failed to initialize OpenTelemetry trace exporter: %s", otel_err)

    # 2. Inject trace_id/span_id into Python log records.
    try:
        _configure_log_correlation()
    except ImportError:
        logger.warning(
            "opentelemetry-instrumentation-logging is not installed. "
            "Log-trace correlation will rely on active span context only."
        )
    except Exception as corr_err:
        logger.error("Failed to enable log correlation instrumentation: %s", corr_err)

    # 3. Export logs via OTLP (linked to distributed traces in Dynatrace).
    try:
        _configure_otlp_log_export(otlp_base_url, otlp_headers, service_name)
    except ImportError:
        logger.warning(
            "OpenTelemetry log exporter packages are not installed. "
            "Skipping OTLP log export."
        )
    except Exception as log_err:
        logger.error("Failed to initialize OTLP log export: %s", log_err)

    # 4. Optional legacy log ingest API (now enriched with trace correlation).
    if os.getenv("DYNATRACE_LOG_INGEST_ENABLED", "true").lower() == "true":
        try:
            handler = DynatraceLogHandler(env_url, dt_token_val, app_name=service_name)
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            )
            handler.setFormatter(formatter)
            logging.getLogger().addHandler(handler)
            logger.info("Dynatrace Log Ingestion Handler configured with trace correlation.")
        except Exception as e:
            logger.error("Failed to initialize Dynatrace log handler: %s", e)


def get_diagnostics_callbacks() -> list:
    """Build callbacks for fallback execution diagnostics.

    Returns:
        A stdout callback list in debug mode, otherwise an empty list.
    """
    callbacks = []
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        callbacks.append(StdOutCallbackHandler())
    return callbacks
