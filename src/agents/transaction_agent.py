"""
Transaction Agent: builds load-test TXNs from the Playwright journey flow,
then attaches meaningful HTTP requests captured during each phase.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

NOISE_HOST_HINTS = (
    "backtrace.io",
    "google-analytics",
    "googletagmanager",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "doubleclick",
    "facebook.net",
    "hotjar",
    "sentry.io",
    "newrelic",
    "segment.io",
    "mixpanel",
    "clarity.ms",
)

STATIC_PATH_HINTS = (
    ".css", ".js", ".map", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
)


def _slug_txn_name(name: str) -> str:
    """Normalize a transaction label for reports and generated scripts.

    Args:
        name: Human-readable transaction name.

    Returns:
        An underscore-delimited label, or ``Transaction`` when empty.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", (name or "").strip()).strip("_")
    return cleaned or "Transaction"


def _short_request_label(req: Dict[str, Any], max_len: int = 120) -> str:
    """Create a bounded method-and-URL label for a captured request.

    Args:
        req: Captured request dictionary.
        max_len: Maximum returned label length.

    Returns:
        A display label retaining the absolute host when available.
    """
    method = (req.get("method") or "GET").upper()
    url = req.get("url") or ""
    try:
        parsed = urlparse(url)
        path = parsed.path or "/"
        if parsed.query and len(parsed.query) <= 48:
            path = f"{path}?{parsed.query}"
        elif parsed.query:
            path = f"{path}?…"
        # Keep scheme+host so k6 / reports can reconstruct absolute URLs
        if parsed.scheme and parsed.netloc:
            label = f"{method} {parsed.scheme}://{parsed.netloc}{path}"
        elif parsed.netloc:
            label = f"{method} {parsed.netloc}{path}"
        else:
            label = f"{method} {path}"
    except Exception:
        label = f"{method} {url}"
    if len(label) > max_len:
        return label[: max_len - 1] + "…"
    return label


def _http_entry(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert an absolute captured request into a structured HTTP entry.

    Args:
        req: Captured request dictionary.

    Returns:
        A normalized HTTP entry, or ``None`` for non-HTTP URLs.
    """
    url = (req.get("url") or "").strip()
    if not url.startswith("http"):
        return None
    return {
        "method": (req.get("method") or "GET").upper(),
        "url": url,
        "resource_type": req.get("resource_type") or "",
        "status": req.get("status"),
        "capture_source": req.get("capture_source") or "",
    }


def _structured_ui_step(step: Dict[str, Any]) -> Dict[str, Any]:
    """Select load-test-relevant fields from a browser step.

    Args:
        step: Raw Playwright journey step.

    Returns:
        A normalized action, selector, value, and URL dictionary.
    """
    return {
        "action": step.get("action"),
        "selector": step.get("selector") or "",
        "value": step.get("value"),
        "url": step.get("url") or "",
    }


def _is_meaningful_http_request(url: str, resource_type: str = "") -> bool:
    """Decide whether a request belongs in a performance transaction.

    Args:
        url: Captured request URL.
        resource_type: Browser resource classification, when available.

    Returns:
        ``True`` for application traffic and ``False`` for static or telemetry
        noise.
    """
    if not url:
        return False
    lower = url.lower().strip()
    if lower.startswith("data:") or lower.startswith("blob:") or lower.startswith("about:"):
        return False
    if any(h in lower for h in NOISE_HOST_HINTS):
        return False

    rt = (resource_type or "").lower()
    if rt in ("stylesheet", "image", "media", "font", "texttrack", "manifest"):
        return False

    path = urlparse(url).path.lower()
    # Allow HTML documents even if path looks static-ish
    if rt in ("document", "xhr", "fetch") or rt in ("Document", "XHR", "Fetch"):
        if any(path.endswith(ext) for ext in (".css", ".woff", ".woff2", ".png", ".jpg", ".svg", ".ico")):
            return False
        return True

    if any(path.endswith(ext) or ext in path for ext in STATIC_PATH_HINTS):
        return False
    return True


def _step_action_label(idx: int, step: Dict[str, Any]) -> str:
    """Format one UI step for transaction reports.

    Args:
        idx: Zero-based journey index, accepted for caller alignment.
        step: Browser action dictionary.

    Returns:
        A concise human-readable UI action.
    """
    action = step.get("action", "step")
    selector = step.get("selector") or ""
    value = step.get("value")
    url = step.get("url") or ""
    if action == "navigate":
        return f"UI navigate → {url}"
    if action == "fill":
        shown = str(value)[:40] if value is not None else ""
        return f"UI fill {selector} = {shown}"
    if action == "click":
        return f"UI click {selector}"
    if action == "select":
        return f"UI select {selector} = {value}"
    if action == "wait_for_selector":
        return f"UI wait_for_selector {selector}"
    if action in ("wait", "wait_for_load"):
        return f"UI {action}"
    return f"UI {action} {selector}".strip()


class TransactionAgent:
    """Builds deterministic load-test transactions from journey phases.

    Ordered phases come from Playwright sub-tasks or inferred step labels.
    Meaningful captured requests are attached by step index, while UI actions
    preserve phases that produce no HTTP traffic.
    """

    def _derive_txn_phases(
        self,
        user_steps: List[Any],
        sub_tasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Derive ordered transaction phases from journey metadata.

        Args:
            user_steps: Ordered browser journey steps.
            sub_tasks: Optional orchestrator-defined journey phases.

        Returns:
            Phase dictionaries containing names, descriptions, and step indices;
            the first phase is always the initial launch.
        """
        phases: List[Dict[str, Any]] = []
        phases.append(
            {
                "name": "Launch",
                "description": "Initial navigation / application landing",
                "step_indices": [-1],
            }
        )

        # Prefer orchestrator sub_tasks order
        if sub_tasks:
            step_map: Dict[str, List[int]] = {t.get("name", f"phase_{i}"): [] for i, t in enumerate(sub_tasks)}
            for idx, step in enumerate(user_steps):
                if not isinstance(step, dict):
                    continue
                sub = step.get("sub_task")
                if sub in step_map:
                    step_map[sub].append(idx)
                elif sub:
                    step_map.setdefault(sub, []).append(idx)

            for task in sub_tasks:
                name = task.get("name") or "phase"
                phases.append(
                    {
                        "name": _slug_txn_name(name),
                        "description": task.get("description")
                        or f"Journey phase: {name}",
                        "step_indices": step_map.get(name, [])
                        or step_map.get(task.get("name"), []),
                    }
                )

            # Any steps with sub_task not covered by orchestrator list
            known = {_slug_txn_name(p["name"]) for p in phases}
            extras: Dict[str, List[int]] = {}
            for idx, step in enumerate(user_steps):
                if not isinstance(step, dict):
                    continue
                sub = step.get("sub_task")
                if not sub:
                    continue
                key = _slug_txn_name(sub)
                if key not in known:
                    extras.setdefault(key, []).append(idx)
            for name, indices in extras.items():
                phases.append(
                    {
                        "name": name,
                        "description": f"Journey phase: {name}",
                        "step_indices": indices,
                    }
                )
            return phases

        # No sub_tasks: group consecutive steps by inferred phase from actions
        if not user_steps:
            return phases

        current_name = None
        current_indices: List[int] = []
        current_desc = ""

        def flush():
            """Append and reset the currently inferred phase, if present."""
            nonlocal current_name, current_indices, current_desc
            if current_name is None:
                return
            phases.append(
                {
                    "name": _slug_txn_name(current_name),
                    "description": current_desc,
                    "step_indices": list(current_indices),
                }
            )
            current_name = None
            current_indices = []
            current_desc = ""

        for idx, step in enumerate(user_steps):
            if not isinstance(step, dict):
                continue
            action = step.get("action", "")
            selector = (step.get("selector") or "").lower()
            # Infer phase boundaries
            if action == "navigate" and idx == 0:
                phase = "Launch"
                desc = "Initial navigation"
            elif "login" in selector or "user-name" in selector or "password" in selector or "signon" in selector:
                phase = "Login"
                desc = "Authenticate user"
            elif "cart" in selector or "add-to-cart" in selector:
                phase = "Add_To_Cart"
                desc = "Add products to cart"
            elif "checkout" in selector or "first-name" in selector or "postal" in selector:
                phase = "Checkout"
                desc = "Checkout and shipping details"
            elif "finish" in selector or "complete" in selector:
                phase = "Order_Complete"
                desc = "Complete order"
            elif "logout" in selector:
                phase = "Logout"
                desc = "End session"
            else:
                phase = current_name or f"Step_Group_{len(phases)}"
                desc = current_desc or f"Journey steps starting at {idx + 1}"

            if current_name is None:
                current_name = phase
                current_desc = desc
                current_indices = [idx]
            elif _slug_txn_name(phase) == _slug_txn_name(current_name):
                current_indices.append(idx)
            else:
                flush()
                current_name = phase
                current_desc = desc
                current_indices = [idx]
        flush()
        return phases

    def group_from_journey(
        self,
        user_steps: List[Any],
        sub_tasks: List[Dict[str, Any]],
        network_requests: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build transaction rows from journey phases and captured traffic.

        Args:
            user_steps: Ordered browser journey steps.
            sub_tasks: Optional orchestrator-defined phases.
            network_requests: Requests tagged with journey step indices.

        Returns:
            Transaction dictionaries containing structured HTTP and UI activity.
        """
        phases = self._derive_txn_phases(user_steps, sub_tasks)

        # Step-index lookup preserves phase ownership even when identical
        # endpoints fire repeatedly during the journey.
        by_step: Dict[int, List[Dict[str, Any]]] = {}
        for req in network_requests or []:
            url = req.get("url") or ""
            if not _is_meaningful_http_request(url, str(req.get("resource_type") or "")):
                continue
            try:
                step_index = int(req.get("step_index", -1))
            except Exception:
                step_index = -1
            by_step.setdefault(step_index, []).append(req)

        transactions: List[Dict[str, Any]] = []
        for phase in phases:
            name = _slug_txn_name(phase["name"])
            desc = phase.get("description") or name
            indices = phase.get("step_indices") or []

            # Playwright actions under this phase
            action_lines: List[str] = []
            ui_steps: List[Dict[str, Any]] = []
            for idx in indices:
                if idx < 0:
                    action_lines.append("UI initial navigation")
                    continue
                if 0 <= idx < len(user_steps) and isinstance(user_steps[idx], dict):
                    action_lines.append(_step_action_label(idx, user_steps[idx]))
                    ui_steps.append(_structured_ui_step(user_steps[idx]))

            # HTTP requests for those steps (structured + labels)
            req_labels: List[str] = []
            http_entries: List[Dict[str, Any]] = []
            seen: Set[str] = set()
            seen_entry: Set[Tuple[str, str]] = set()
            for idx in indices:
                for req in by_step.get(idx, []):
                    entry = _http_entry(req)
                    if entry:
                        key = (entry["method"], entry["url"])
                        if key not in seen_entry:
                            seen_entry.add(key)
                            http_entries.append(entry)
                    label = _short_request_label(req)
                    if label in seen:
                        continue
                    seen.add(label)
                    req_labels.append(label)

            # What to show in the Requests column — prefer real HTTP over UI noise
            if req_labels:
                display = req_labels
            elif action_lines:
                display = action_lines[:12]
            else:
                display = ["(no HTTP / UI activity captured)"]

            transactions.append(
                {
                    "name": name,
                    "description": desc,
                    "request_urls": display,
                    "http_requests": req_labels,
                    "http_entries": http_entries,
                    "ui_actions": action_lines,
                    "ui_steps": ui_steps,
                    "step_indices": indices,
                }
            )

        # Drop Launch only if it has nothing and we have other phases with content
        cleaned = []
        for t in transactions:
            if (
                t["name"] == "Launch"
                and t["request_urls"] == ["(no HTTP / UI activity captured)"]
                and len(transactions) > 1
            ):
                # Keep Launch if there are real launch HTTP requests; else skip empty
                continue
            cleaned.append(t)

        logger.info(
            "TransactionAgent built %s TXNs from Playwright journey (%s with HTTP).",
            len(cleaned),
            sum(1 for t in cleaned if t.get("http_requests")),
        )
        return cleaned

    async def group_transactions(
        self,
        target_url: str,
        user_steps: List[Any],
        sub_tasks: List[Dict[str, Any]],
        network_requests: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build deterministic transactions without LLM inference.

        Args:
            target_url: Analyzed application URL, accepted for API consistency.
            user_steps: Ordered browser journey steps.
            sub_tasks: Optional orchestrator-defined phases.
            network_requests: Requests tagged with journey step indices.

        Returns:
            Transaction dictionaries produced from journey and capture evidence.
        """
        return self.group_from_journey(user_steps, sub_tasks, network_requests)

    # Back-compat alias used by graph fallback
    def _heuristic_group(
        self,
        requests: List[Dict[str, Any]],
        user_steps: List[Any],
        sub_tasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Provide the legacy graph alias for deterministic grouping.

        Args:
            requests: Captured network requests.
            user_steps: Ordered browser journey steps.
            sub_tasks: Optional orchestrator-defined phases.

        Returns:
            Transaction dictionaries from :meth:`group_from_journey`.
        """
        return self.group_from_journey(user_steps, sub_tasks, requests)
