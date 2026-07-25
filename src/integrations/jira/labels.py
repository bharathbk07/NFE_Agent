"""Lifecycle and routing labels for the NFE Jira worker."""

from config.settings import settings

LABEL_AGENT = "nfe-agent"
LABEL_QUEUED = "nfe-queued"
LABEL_RUNNING = "nfe-running"
LABEL_RECORDING_READY = "nfe-recording-ready"
LABEL_DONE = "nfe-done"
LABEL_BLOCKED = "nfe-blocked"


def routing_label() -> str:
    """Return the configured routing label (default ``nfe-agent``)."""
    return (settings.NFE_JIRA_LABEL or LABEL_AGENT).strip() or LABEL_AGENT
