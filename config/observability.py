"""
Module: Dual Observability System Configurator
Description: Integrates LangSmith tracing configurations alongside standard 
             OpenTelemetry metrics and Dynatrace Log Ingestion API.
"""
import os
import logging
import queue
import threading
import time
import requests
from langchain_core.callbacks import StdOutCallbackHandler

logger = logging.getLogger(__name__)

class DynatraceLogHandler(logging.Handler):
    """
    Custom logging handler that asynchronously sends logs in batches 
    to the Dynatrace Log Ingestion API v2.
    """
    def __init__(self, base_url: str, api_token: str, app_name: str = "nfe-agent"):
        super().__init__()
        self.url = f"{base_url.rstrip('/')}/api/v2/logs/ingest"
        self.headers = {
            "Authorization": f"Api-Token {api_token}",
            "Content-Type": "application/json; charset=utf-8"
        }
        self.app_name = app_name
        self.queue = queue.Queue()
        self.worker = threading.Thread(target=self._post_logs_worker, daemon=True)
        self.worker.start()

    def emit(self, record):
        try:
            log_entry = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(record.created)),
                "content": self.format(record),
                "severity": record.levelname,
                "log.source": record.name,
                "service.name": self.app_name
            }
            self.queue.put(log_entry)
        except Exception:
            self.handleError(record)

    def _post_logs_worker(self):
        while True:
            batch = []
            try:
                # Wait for at least one item, then collect more up to a limit
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
                    response = requests.post(self.url, json=batch, headers=self.headers, timeout=5)
                    response.raise_for_status()
                except Exception:
                    # Never panic - fallback gracefully by ignoring network/auth ingestion errors
                    pass

def initialize_observability() -> None:
    """
    Initializes telemetries globally. Configures both the local LangChain
    tracing flags, standard OpenTelemetry, and Dynatrace log exporter.
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

    dt_token_val = ""
    if dt_token:
        if "Api-Token=" in dt_token:
            dt_token_val = dt_token.split("Api-Token=")[1].strip()
        else:
            dt_token_val = dt_token.strip()

    if dt_api_url and dt_token_val:
        # A. Configure Dynatrace Log Ingestion
        try:
            handler = DynatraceLogHandler(dt_api_url, dt_token_val)
            formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
            handler.setFormatter(formatter)
            logging.getLogger().addHandler(handler)
            logger.info("Dynatrace Log Ingestion Handler configured successfully.")
        except Exception as e:
            logger.error("Failed to initialize Dynatrace log handler: %s", e)

        # B. Configure OpenLLMetry Tracing
        try:
            from traceloop.sdk import Traceloop
            Traceloop.init(
                app_name=os.getenv("LANGCHAIN_PROJECT", "mcp-agent-service"),
                disable_batches=False,
                traceloop_sync_enabled=False
            )
            logger.info("Dynatrace OpenLLMetry integration configured successfully.")
        except ImportError:
            logger.warning("traceloop-sdk is not installed. Skipping Dynatrace OpenLLMetry initialization.")
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
