"""Emit lightweight progress events so Studio SSE does not idle-timeout.

LangGraph Studio reconnects when no SSE bytes arrive for ~15s. Long k6 /
Playwright work is silent unless we stream custom progress.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)


def emit_progress(message: str, *, phase: str = "progress") -> None:
    """Best-effort custom stream event (no-op outside a LangGraph run)."""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if writer is None:
            return
        writer({"nfe": True, "phase": phase, "message": str(message)[:500]})
    except Exception:
        # Outside graph / older runtime — ignore
        pass


@contextmanager
def progressive_wait(
    label: str,
    *,
    interval_s: float = 5.0,
) -> Iterator[None]:
    """While a blocking section runs, emit SSE progress every ``interval_s``."""

    stop = threading.Event()

    def _ping() -> None:
        n = 0
        while not stop.wait(max(1.0, interval_s)):
            n += 1
            emit_progress(f"{label}… still running ({n * int(interval_s)}s)", phase="heartbeat")

    thread = threading.Thread(target=_ping, name="nfe-sse-heartbeat", daemon=True)
    emit_progress(f"{label}… started", phase="heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1.0)
        emit_progress(f"{label}… finished", phase="heartbeat")
