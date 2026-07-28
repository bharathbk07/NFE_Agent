"""Find-or-create Confluence page hierarchy helpers."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from src.integrations.confluence.client import ConfluenceClient
from src.integrations.confluence.security import sanitize_title

logger = logging.getLogger(__name__)


def find_or_create_page(
    client: ConfluenceClient,
    *,
    title: str,
    parent_id: Optional[str],
    storage_body: str,
) -> Tuple[Dict[str, Any], bool]:
    """Return ``(page, created)`` for a title under an optional parent."""
    safe_title = sanitize_title(title)
    existing = client.find_page_by_title(safe_title, parent_id=parent_id)
    if existing:
        return existing, False
    created = client.create_page(
        title=safe_title,
        storage_body=storage_body,
        parent_id=parent_id,
    )
    logger.info(
        "Created Confluence page %s (%s) under parent=%s",
        created.get("id"),
        safe_title,
        parent_id,
    )
    return created, True


def update_page_body(
    client: ConfluenceClient,
    page: Dict[str, Any],
    *,
    title: Optional[str] = None,
    storage_body: str,
) -> Dict[str, Any]:
    """Bump version and replace storage body."""
    page_id = str(page.get("id") or "")
    if not page_id:
        raise ValueError("page id missing")
    # Refresh version if thin search result
    version = ((page.get("version") or {}).get("number"))
    current_title = page.get("title") or title or "Untitled"
    if version is None:
        fresh = client.get_page(page_id, expand="version")
        version = ((fresh.get("version") or {}).get("number")) or 1
        current_title = fresh.get("title") or current_title
    return client.update_page(
        page_id=page_id,
        title=sanitize_title(title or current_title),
        storage_body=storage_body,
        version_number=int(version),
    )
