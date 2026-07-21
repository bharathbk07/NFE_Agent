"""
Transaction Agent: builds load-test TXNs from the Playwright journey flow,
then attaches meaningful HTTP requests captured during each phase.

User-facing reports show **business / user steps** only. Full HTTP lists are
kept on the transaction objects for IR/k6 generation — not for chat display.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, unquote

from src.utils.correlation_noise import is_login_field_selector

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

# SPA chrome / telemetry — keep out of k6 protocol TXNs
SPA_CHROME_PATH_HINTS = (
    "/i18n/",
    "/core/i18n",
    "/buzz/",
    "/events/push",
    "/dashboard/shortcuts",
    "/dashboard/employees/action-sum",
    "/dashboard/employees/subunit",
    "/dashboard/employees/locations",
    "/dashboard/employees/time-at-wo",
    "/dashboard/employees/leaves",
    "/leave/workweek",
    "/leave/holidays",
    "/pim/viewphoto",
    "fonts.",
)

# Single-bucket orchestrator names that should not become one mega-TXN.
GENERIC_PHASE_NAMES = frozenset({
    "watch_me_flow",
    "main_flow",
    "phase",
    "journey",
    "flow",
    "step_group_1",
})

# Path leaf → (txn name, business description)
PATH_PHASE_HINTS: Tuple[Tuple[str, str, str], ...] = (
    ("login", "Login", "Sign in to the application"),
    ("auth", "Login", "Authenticate"),
    ("dashboard", "Dashboard", "Open home / dashboard"),
    ("viewassignclaim", "View_Claims", "Open claims list"),
    ("assignclaim", "Assign_Claim", "Create or assign a claim"),
    ("claim", "Claims", "Work with claims"),
    ("pim", "Employee_Search", "Search / select employee"),
    ("leave", "Leave", "Leave-related activity"),
    ("buzz", "Buzz", "Social / buzz feed"),
    ("checkout", "Checkout", "Checkout"),
    ("cart", "Cart", "Cart"),
    ("logout", "Logout", "Sign out"),
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
    if rt in ("document", "xhr", "fetch") or rt in ("Document", "XHR", "Fetch"):
        if any(path.endswith(ext) for ext in (".css", ".woff", ".woff2", ".png", ".jpg", ".svg", ".ico")):
            return False
        return True

    if any(path.endswith(ext) or ext in path for ext in STATIC_PATH_HINTS):
        return False
    return True


def _is_business_critical_http(url: str, method: str = "GET") -> bool:
    """Prefer mutating / auth / claim APIs for k6; drop SPA dashboard chrome."""
    lower = (url or "").lower()
    method_u = (method or "GET").upper()
    if any(h in lower for h in SPA_CHROME_PATH_HINTS):
        return False
    if method_u in ("POST", "PUT", "PATCH", "DELETE"):
        return True
    # Auth / claim / employee search documents and APIs
    critical_hints = (
        "/auth/",
        "/login",
        "/claim/",
        "/api/v2/claim",
        "/api/v2/pim/employees",
        "/assignclaim",
        "/viewassignclaim",
    )
    if any(h in lower for h in critical_hints):
        return True
    # Landing document navigations
    path = urlparse(url).path.lower()
    if path.endswith("/dashboard/index") or path.endswith("/auth/login"):
        return True
    return False


def _filter_http_for_k6(
    entries: List[Dict[str, Any]],
    *,
    max_gets: int = 8,
) -> List[Dict[str, Any]]:
    """Keep business-critical HTTP for scripting; cap leftover GETs."""
    critical: List[Dict[str, Any]] = []
    other_gets: List[Dict[str, Any]] = []
    for e in entries:
        url = e.get("url") or ""
        method = (e.get("method") or "GET").upper()
        if _is_business_critical_http(url, method):
            critical.append(e)
        elif method == "GET":
            other_gets.append(e)
    # Prefer critical; only keep a few other GETs if phase would otherwise be empty
    if critical:
        return critical
    return other_gets[:max_gets]


def _human_field_name(selector: str) -> str:
    """Derive a short field label from a selector."""
    sel = selector or ""
    for pattern in (
        r'name=["\']?([^"\'\]]+)',
        r'id=["\']?([^"\'\]]+)',
        r'placeholder\*?=["\']?([^"\']+)',
        r'aria-label=["\']?([^"\']+)',
        r'data-testid=["\']?([^"\']+)',
        r'#([\w-]+)',
    ):
        m = re.search(pattern, sel, re.IGNORECASE)
        if m:
            label = re.sub(r"[_\-]+", " ", m.group(1)).strip()
            if label and not label.isdigit():
                return label
    if "password" in sel.lower():
        return "password"
    if "textarea" in sel.lower():
        return "notes"
    if "input" in sel.lower():
        return "input"
    return "field"


def _business_action_label(step: Dict[str, Any]) -> str:
    """Format one UI step as a business/user action (not CSS/HTTP).

    Args:
        step: Browser action dictionary.

    Returns:
        A concise human-readable user action.
    """
    action = (step.get("action") or "step").lower()
    selector = step.get("selector") or ""
    value = step.get("value")
    url = step.get("url") or ""

    if action == "txn_start":
        return f"Start transaction ({value or step.get('sub_task') or 'TXN'})"
    if action == "txn_end":
        return f"End transaction ({value or step.get('sub_task') or 'TXN'})"

    if action == "navigate":
        _name, desc = _phase_from_url(url)
        # Prefer short business phrase: "Open claims list" from description
        if desc.lower().startswith("open ") or desc.lower().startswith("sign "):
            return desc if desc[0].isupper() else desc[:1].upper() + desc[1:]
        if desc.lower().startswith("create"):
            return desc
        leaf = _path_leaf(url) or "page"
        nice = re.sub(r"([a-z])([A-Z])", r"\1 \2", leaf)
        nice = re.sub(r"[_\-]+", " ", nice).strip()
        return f"Open {nice}" if nice else "Open page"
    if action == "fill":
        field = _human_field_name(selector)
        if is_login_field_selector(selector) or field.lower() in ("username", "user", "password"):
            return f"Enter {field}"
        shown = str(value).strip() if value is not None else ""
        if shown and len(shown) <= 24 and not shown.isdigit():
            return f"Enter {field} ({shown})"
        return f"Enter {field}"
    if action == "select":
        return f"Select {_human_field_name(selector)}"
    if action == "click":
        field = _human_field_name(selector)
        # Prefer button-ish wording when selector is opaque CSS
        if field in ("field", "input") or ">" in selector:
            return "Click action"
        return f"Click {field}"
    if action == "wait_for_selector":
        return f"Wait for {_human_field_name(selector)}"
    if action in ("wait", "wait_for_load"):
        return "Wait for page"
    return f"{action.replace('_', ' ').title()}"


def _collapse_business_actions(actions: List[str]) -> List[str]:
    """Collapse typeahead spam and consecutive duplicate actions.

    Args:
        actions: Ordered business-action labels.

    Returns:
        Deduplicated labels suitable for a short TXN report.
    """
    if not actions:
        return []
    out: List[str] = []
    for label in actions:
        if out and out[-1] == label:
            continue
        # Collapse "Enter name (j)" / "Enter name (jo)" typeahead into one line
        if out and out[-1].startswith("Enter ") and label.startswith("Enter "):
            prev_field = out[-1].split("(", 1)[0].strip()
            cur_field = label.split("(", 1)[0].strip()
            if prev_field == cur_field:
                out[-1] = f"{prev_field} (typeahead)"
                continue
        out.append(label)
    # Cap for readability — engineers want the story, not every click
    if len(out) > 8:
        head, tail = out[:6], out[-1]
        return head + [f"… (+{len(out) - 7} more user actions)", tail]
    return out


def _path_leaf(url: str) -> str:
    """Return a readable leaf from a URL path."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if not parts:
            return ""
        # skip pure ids
        leaf = parts[-1]
        if leaf.isdigit() and len(parts) >= 2:
            leaf = parts[-2]
        return unquote(leaf)
    except Exception:
        return ""


def _phase_from_url(url: str) -> Tuple[str, str]:
    """Map a page URL to a business transaction name and description."""
    if not url:
        return "Page", "Continue journey"
    path = urlparse(url).path.lower()
    for hint, name, desc in PATH_PHASE_HINTS:
        if hint in path:
            # Prefer more specific claim pages over generic "claim"
            if hint == "claim" and ("viewassign" in path or "assignclaim" in path):
                continue
            return name, desc
    leaf = _path_leaf(url) or "page"
    nice = re.sub(r"([a-z])([A-Z])", r"\1 \2", leaf)
    nice = re.sub(r"[_\-]+", " ", nice).strip().title() or "Page"
    return _slug_txn_name(nice), f"Complete: {nice}"


def _subtasks_are_generic(sub_tasks: List[Dict[str, Any]]) -> bool:
    """Return True when sub-tasks would produce one meaningless mega-TXN."""
    if not sub_tasks:
        return True
    if len(sub_tasks) == 1:
        name = _slug_txn_name(sub_tasks[0].get("name") or "").lower()
        return name in GENERIC_PHASE_NAMES or "watch_me" in name
    return False


class TransactionAgent:
    """Builds deterministic load-test transactions from journey phases.

    Ordered phases come from Playwright sub-tasks or inferred navigation/login
    boundaries. Meaningful captured requests are attached by step index for
    scripting; user-facing reports use business steps only.
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
                "description": "Open application / landing page",
                "step_indices": [-1],
            }
        )

        # Prefer real multi-phase orchestrator plans; ignore watch_me mega-bucket.
        if sub_tasks and not _subtasks_are_generic(sub_tasks):
            step_map: Dict[str, List[int]] = {
                t.get("name", f"phase_{i}"): [] for i, t in enumerate(sub_tasks)
            }
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
            return phases

        # Watch-me user TXN markers (Start TXN / End TXN) beat URL inference.
        if self._has_user_txn_markers(user_steps):
            return phases + self._phases_from_txn_markers(user_steps)

        return phases + self._phases_from_user_journey(user_steps)

    @staticmethod
    def _has_user_txn_markers(user_steps: List[Any]) -> bool:
        """Return True when the journey includes explicit TXN start/end markers."""
        for step in user_steps or []:
            if not isinstance(step, dict):
                continue
            action = (step.get("action") or "").lower()
            if action in ("txn_start", "txn_end"):
                return True
            sub = str(step.get("sub_task") or "").strip()
            if sub and sub.lower() not in GENERIC_PHASE_NAMES and "watch_me" not in sub.lower():
                # Tagged with a real TXN name during recording
                if action and action not in ("initial_navigation",):
                    return True
        return False

    def _phases_from_txn_markers(self, user_steps: List[Any]) -> List[Dict[str, Any]]:
        """Build phases from Watch-me Start TXN / End TXN markers and tags.

        Args:
            user_steps: Ordered recorded steps that include txn_start/txn_end
                and/or non-generic ``sub_task`` labels.

        Returns:
            Phase dictionaries without the Launch sentinel.
        """
        phases: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None

        def flush() -> None:
            nonlocal current
            if current and current.get("step_indices"):
                phases.append(current)
            current = None

        for idx, step in enumerate(user_steps or []):
            if not isinstance(step, dict):
                continue
            action = (step.get("action") or "").lower()
            if action == "txn_start":
                flush()
                name = str(step.get("value") or step.get("sub_task") or "Transaction").strip()
                current = {
                    "name": _slug_txn_name(name),
                    "description": f"User-defined transaction: {name}",
                    "step_indices": [idx],
                }
                continue
            if action == "txn_end":
                if current is None:
                    name = str(step.get("value") or step.get("sub_task") or "Transaction").strip()
                    current = {
                        "name": _slug_txn_name(name),
                        "description": f"User-defined transaction: {name}",
                        "step_indices": [idx],
                    }
                else:
                    current["step_indices"].append(idx)
                flush()
                continue

            sub = str(step.get("sub_task") or "").strip()
            if current is None:
                if sub and sub.lower() not in GENERIC_PHASE_NAMES and "watch_me" not in sub.lower():
                    current = {
                        "name": _slug_txn_name(sub),
                        "description": f"User-defined transaction: {sub}",
                        "step_indices": [idx],
                    }
                # Untagged steps before first Start TXN stay out of user TXNs
                continue

            # New tag without txn_start → close previous and open next
            if (
                sub
                and sub.lower() not in GENERIC_PHASE_NAMES
                and "watch_me" not in sub.lower()
                and _slug_txn_name(sub) != current["name"]
            ):
                flush()
                current = {
                    "name": _slug_txn_name(sub),
                    "description": f"User-defined transaction: {sub}",
                    "step_indices": [idx],
                }
                continue

            current["step_indices"].append(idx)

        flush()
        return phases

    def _phases_from_user_journey(self, user_steps: List[Any]) -> List[Dict[str, Any]]:
        """Split a flat journey into business phases by login/navigation cues.

        Args:
            user_steps: Ordered browser steps (e.g. Watch-me recording).

        Returns:
            Phase dictionaries without the Launch sentinel.
        """
        if not user_steps:
            return []

        phases: List[Dict[str, Any]] = []
        current_name: Optional[str] = None
        current_desc = ""
        current_indices: List[int] = []

        def flush() -> None:
            nonlocal current_name, current_desc, current_indices
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
            current_desc = ""
            current_indices = []

        def start_phase(name: str, desc: str, idx: int) -> None:
            nonlocal current_name, current_desc, current_indices
            if current_name and _slug_txn_name(name) == _slug_txn_name(current_name):
                current_indices.append(idx)
                return
            flush()
            current_name = name
            current_desc = desc
            current_indices = [idx]

        for idx, step in enumerate(user_steps):
            if not isinstance(step, dict):
                continue
            action = (step.get("action") or "").lower()
            selector = step.get("selector") or ""
            url = step.get("url") or ""

            if is_login_field_selector(selector) or (
                action in ("fill", "click")
                and any(tok in selector.lower() for tok in ("login", "password", "username", "signon"))
            ):
                start_phase("Login", "Sign in to the application", idx)
                continue

            if action == "navigate" and url:
                name, desc = _phase_from_url(url)
                start_phase(name, desc, idx)
                continue

            # Stay in current phase; bootstrap a generic one if needed
            if current_name is None:
                if action == "click":
                    start_phase("Interact", "User interaction", idx)
                else:
                    start_phase("Journey", "User journey steps", idx)
            else:
                current_indices.append(idx)

        flush()
        return phases

    def _collapse_revisited_transactions(
        self, transactions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Merge revisit phases into the first occurrence; drop trailing Login.

        Watch-me journeys often re-open Claims / Login. Product output should
        show one business flow, not every navigation bounce.
        """
        if not transactions:
            return transactions

        by_name: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for t in transactions:
            name = str(t.get("name") or "Phase")
            base = re.sub(r"_\d+$", "", name)
            if base not in by_name:
                by_name[base] = dict(t)
                by_name[base]["name"] = base
                order.append(base)
                continue
            # Merge HTTP + UI from revisit into first phase
            primary = by_name[base]
            seen = {
                (e.get("method"), e.get("url"))
                for e in (primary.get("http_entries") or [])
                if isinstance(e, dict)
            }
            for e in t.get("http_entries") or []:
                if not isinstance(e, dict):
                    continue
                key = (e.get("method"), e.get("url"))
                if key in seen:
                    continue
                seen.add(key)
                primary.setdefault("http_entries", []).append(e)
            for s in t.get("ui_steps") or []:
                primary.setdefault("ui_steps", []).append(s)
            for step in t.get("business_steps") or []:
                primary.setdefault("business_steps", [])
                if step not in primary["business_steps"]:
                    primary["business_steps"].append(step)
            primary["ui_actions"] = list(primary.get("business_steps") or [])
            primary["request_urls"] = list(primary.get("business_steps") or [])
            primary["http_requests"] = [
                f"{e.get('method')} {(e.get('url') or '')[:60]}"
                for e in (primary.get("http_entries") or [])[:12]
            ]
            primary["step_indices"] = list(
                dict.fromkeys(
                    list(primary.get("step_indices") or [])
                    + list(t.get("step_indices") or [])
                )
            )

        collapsed = [by_name[n] for n in order]

        # Drop trailing Login revisit (logout/login page bounce)
        while (
            len(collapsed) > 1
            and str(collapsed[-1].get("name") or "").lower() == "login"
            and any(str(t.get("name") or "").lower() == "login" for t in collapsed[:-1])
        ):
            collapsed.pop()

        return collapsed

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
            Transaction dictionaries. ``business_steps`` / ``ui_actions`` are for
            reports; ``http_entries`` / ``http_requests`` are for IR/k6 only.
        """
        phases = self._derive_txn_phases(user_steps, sub_tasks)

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

            action_lines: List[str] = []
            ui_steps: List[Dict[str, Any]] = []
            for idx in indices:
                if idx < 0:
                    action_lines.append("Open landing page")
                    continue
                if 0 <= idx < len(user_steps) and isinstance(user_steps[idx], dict):
                    action_lines.append(_business_action_label(user_steps[idx]))
                    act = (user_steps[idx].get("action") or "").lower()
                    if act not in ("txn_start", "txn_end"):
                        ui_steps.append(_structured_ui_step(user_steps[idx]))

            business_steps = _collapse_business_actions(action_lines)

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

            # k6/IR: business-critical only; chat uses business_steps
            http_entries = _filter_http_for_k6(http_entries)
            req_labels = [
                _short_request_label({"method": e["method"], "url": e["url"]})
                for e in http_entries
            ]

            # User-facing summary: business steps only (never raw URL dumps).
            if business_steps:
                display = business_steps
            elif http_entries:
                # Fallback when UI was empty but HTTP exists (rare)
                display = [f"{len(http_entries)} HTTP call(s) in this phase"]
            else:
                display = ["(no user activity captured)"]

            transactions.append(
                {
                    "name": name,
                    "description": desc,
                    "business_steps": business_steps,
                    "request_urls": display,
                    "http_requests": req_labels,
                    "http_entries": http_entries,
                    "ui_actions": business_steps,
                    "ui_steps": ui_steps,
                    "step_indices": indices,
                }
            )

        cleaned = []
        for t in transactions:
            if (
                t["name"] == "Launch"
                and t["request_urls"] == ["(no user activity captured)"]
                and not t.get("http_entries")
                and len(transactions) > 1
            ):
                continue
            cleaned.append(t)

        cleaned = self._collapse_revisited_transactions(cleaned)

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
