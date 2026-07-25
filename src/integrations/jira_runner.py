"""CLI for the NFE Jira worker (issue / poll / auth check)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from config.observability import initialize_observability
from config.settings import settings

logging.basicConfig(
    level=logging.INFO if not settings.DEBUG_MODE else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("NFE_JiraRunner")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NFE Agent Jira worker / CLI")
    parser.add_argument("--issue", help="Process a single issue key (e.g. SCRUM-1)")
    parser.add_argument(
        "--poll-once",
        action="store_true",
        help="Run one JQL poll using NFE_JIRA_POLL_JQL",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-run even if nfe-done / nfe-running",
    )
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="Verify JIRA_EMAIL + JIRA_API_TOKEN via GET /myself",
    )
    args = parser.parse_args(argv)
    initialize_observability()

    if args.check_auth:
        from src.integrations.jira.client import JiraAPIError, JiraClient

        try:
            me = JiraClient().verify_auth()
            logger.info(
                "Jira auth OK as %s (%s)",
                me.get("displayName") or me.get("emailAddress"),
                me.get("accountId", "")[:12],
            )
            return 0
        except JiraAPIError as exc:
            logger.error("%s", exc)
            return 1
        except ValueError as exc:
            logger.error("%s", exc)
            return 1

    if args.issue:
        from src.integrations.jira.worker import process_issue_key

        key = args.issue.strip()
        if key.lower() in ("nfe-agent", "nfe-recording-ready", "nfe-done", "nfe-blocked"):
            logger.error(
                "'%s' is a Jira *label*, not an issue key. "
                "Use something like SCRUM-1, or chat: work on SCRUM-1. "
                "Or run: .venv/bin/python -m src.integrations.jira_runner --poll-once",
                key,
            )
            return 2

        result = asyncio.run(process_issue_key(key, force=args.force))
        if result.get("error"):
            logger.error("%s", result["error"])
            return 1
        if result.get("skipped"):
            logger.info("Skipped %s: %s", key, result.get("reason"))
            return 0
        if result.get("blocked"):
            logger.warning(
                "Blocked %s: %s", key, result.get("reason") or result.get("errors")
            )
            return 0
        logger.info("Completed %s ok=%s", key, result.get("ok"))
        return 0 if result.get("ok") else 1

    if args.poll_once:
        from src.integrations.jira.client import JiraAPIError
        from src.integrations.jira.worker import poll_once

        try:
            results = asyncio.run(poll_once(force=args.force))
        except JiraAPIError as exc:
            logger.error("%s", exc)
            return 1
        logger.info("Polled %s issue(s)", len(results))
        return 0

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
