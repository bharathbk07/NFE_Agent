"""Capture browser journeys and network details through Playwright and CDP."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, Response, Request

from src.utils.http_body import content_type_from_headers, parse_post_data
from src.utils.prompt_loader import render_prompt

logger = logging.getLogger(__name__)

# Keep documents / XHR / fetch / websockets for load-test analysis.
KEEP_RESOURCE_TYPES = {
    "Document",
    "XHR",
    "Fetch",
    "Script",  # sometimes carries SPA bootstrap tokens — filtered later if static
    "Other",
}

STATIC_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".woff", ".woff2",
    ".svg", ".ico", ".ttf", ".otf", ".eot", ".mp4", ".mp3", ".wav",
    ".less", ".sass", ".scss", ".pdf", ".zip", ".tar", ".gz", ".map",
)

TRACKING_KEYWORDS = (
    "google-analytics", "doubleclick", "hotjar", "mixpanel",
    "segment.io", "facebook.net", "sentry.io", "telemetry",
    "log-delivery", "pixel", "beacon", "backtrace.io",
    "googletagmanager", "clarity.ms",
)


def _should_keep_url(url: str, resource_type: str = "") -> bool:
    """Decide whether a request is relevant to load-test analysis.

    Args:
        url: Requested URL.
        resource_type: CDP or Playwright resource type.

    Returns:
        ``True`` for application traffic and ``False`` for static or tracking data.
    """
    lower = (url or "").lower()
    if lower.startswith("data:") or lower.startswith("blob:") or lower.startswith("about:"):
        return False
    if any(kw in lower for kw in TRACKING_KEYWORDS):
        return False
    # Always keep document / xhr / fetch
    if resource_type in ("Document", "XHR", "Fetch", "document", "xhr", "fetch"):
        if any(ext in lower.split("?")[0] for ext in (".css", ".png", ".jpg", ".woff", ".svg", ".ico")):
            return False
        return True
    if "fonts.googleapis.com" in lower or "fonts.gstatic.com" in lower:
        return False
    if any(ext in lower.split("?")[0] for ext in STATIC_EXTENSIONS):
        return False
    return True


class PlaywrightBrowserRecorder:
    """Execute browser steps and collect step-attributed HTTP traffic."""

    def __init__(self, debug_mode: bool = False):
        """Initialize an empty recorder.

        Args:
            debug_mode: Whether to launch a visible browser and take step screenshots.
        """
        self.debug_mode = debug_mode
        self.network_logs: List[Dict[str, Any]] = []
        self.step_timeline: List[Dict[str, Any]] = []
        self._cdp_pending: Dict[str, Dict[str, Any]] = {}
        self._cdp_session = None
        self.current_step_index = -1
        self.current_step_action = "unknown"

    def _ensure_page_document(
        self,
        page: Page,
        *,
        step_index: int,
        step_action: str,
        reason: str = "page_url",
    ) -> None:
        """Record a synthetic document request when navigation capture is absent.

        Args:
            page: Active Playwright page.
            step_index: Journey step owning the navigation.
            step_action: Action associated with the navigation.
            reason: Capture-source label for the synthetic entry.
        """
        try:
            url = (page.url or "").strip()
        except Exception:
            return
        if not url.startswith("http"):
            return
        for log in self.network_logs:
            if (
                log.get("step_index") == step_index
                and log.get("url") == url
                and str(log.get("resource_type") or "").lower() == "document"
            ):
                return
        self.network_logs.append({
            "url": url,
            "method": "GET",
            "headers": {},
            "cookies": [],
            "post_data": None,
            "response_headers": {},
            "response_body": "",
            "status": 200,
            "resource_type": "Document",
            "initiator_type": "navigation",
            "mime_type": "text/html",
            "step_index": step_index,
            "step_action": step_action,
            "capture_source": reason,
        })

    def _settle_after_action(self, page: Page, action: str) -> None:
        """Wait briefly for action-triggered navigation and requests.

        Args:
            page: Active Playwright page.
            action: Just-completed journey action.
        """
        try:
            if action in ("click", "navigate", "select", "wait_for_load"):
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(400)
            if action in ("click", "navigate", "wait_for_load"):
                try:
                    page.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
        except Exception:
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass

    # ------------------------------------------------------------------ CDP
    def _attach_cdp(self, page: Page) -> None:
        """Attach CDP network listeners, retaining Playwright as fallback.

        Args:
            page: Active Chromium page.
        """
        try:
            self._cdp_session = page.context.new_cdp_session(page)
            self._cdp_session.send("Network.enable", {
                "maxPostDataSize": 256 * 1024,
                "maxResourceBufferSize": 512 * 1024,
            })
            self._cdp_session.on("Network.requestWillBeSent", self._on_cdp_request)
            self._cdp_session.on("Network.responseReceived", self._on_cdp_response)
            self._cdp_session.on("Network.loadingFinished", self._on_cdp_loading_finished)
            logger.info("CDP Network domain enabled for DevTools-grade request capture.")
        except Exception as e:
            logger.warning("Failed to attach CDP Network session (%s); using Playwright listeners only.", e)
            self._cdp_session = None

    def _on_cdp_request(self, params: Dict[str, Any]) -> None:
        """Stage a filtered CDP request until its response completes.

        Args:
            params: ``Network.requestWillBeSent`` event payload.
        """
        try:
            request_id = params.get("requestId")
            req = params.get("request") or {}
            url = req.get("url", "")
            resource_type = params.get("type") or ""
            if not request_id or not _should_keep_url(url, resource_type):
                return

            headers = req.get("headers") or {}
            post_data = req.get("postData")
            parsed_post, body_type = parse_post_data(
                post_data, content_type_from_headers(headers)
            )
            cookie_header = ""
            for hk, hv in headers.items():
                if str(hk).lower() == "cookie":
                    cookie_header = str(hv or "")
                    break
            req_cookies = []
            if cookie_header:
                for part in cookie_header.split(";"):
                    part = part.strip()
                    if "=" in part:
                        n, _, v = part.partition("=")
                        req_cookies.append({"name": n.strip(), "value": v.strip()})

            initiator = params.get("initiator") or {}
            # Freeze step tagging at request time — do not retag on response
            # (late XHR would otherwise land in the wrong TXN).
            self._cdp_pending[request_id] = {
                "request_id": request_id,
                "url": url,
                "method": req.get("method", "GET"),
                "headers": headers,
                "cookies": req_cookies,
                "post_data": parsed_post,
                "body_type": body_type,
                "response_headers": {},
                "response_body": "",
                "status": 0,
                "resource_type": resource_type,
                "initiator_type": initiator.get("type"),
                "initiator_url": (initiator.get("url") or ""),
                "mime_type": "",
                "timing": {},
                "step_index": self.current_step_index,
                "step_action": self.current_step_action,
                "capture_source": "cdp",
            }
        except Exception as e:
            logger.debug("CDP requestWillBeSent handler error: %s", e)

    def _on_cdp_response(self, params: Dict[str, Any]) -> None:
        """Merge response metadata into a pending CDP request.

        Args:
            params: ``Network.responseReceived`` event payload.
        """
        try:
            request_id = params.get("requestId")
            if not request_id or request_id not in self._cdp_pending:
                return
            response = params.get("response") or {}
            entry = self._cdp_pending[request_id]
            entry["status"] = response.get("status", 0)
            entry["response_headers"] = response.get("headers") or {}
            entry["mime_type"] = response.get("mimeType") or ""
            entry["timing"] = response.get("timing") or {}
            # Intentionally keep step_index/step_action from requestWillBeSent
        except Exception as e:
            logger.debug("CDP responseReceived handler error: %s", e)

    def _on_cdp_loading_finished(self, params: Dict[str, Any]) -> None:
        """Finalize a CDP request and capture a supported response body.

        Args:
            params: ``Network.loadingFinished`` event payload.
        """
        try:
            request_id = params.get("requestId")
            if not request_id or request_id not in self._cdp_pending:
                return
            entry = self._cdp_pending.pop(request_id)

            # Prefer JSON/text bodies for correlation
            mime = (entry.get("mime_type") or "").lower()
            if self._cdp_session and (
                "json" in mime or "text" in mime or "javascript" in mime or "xml" in mime or not mime
            ):
                try:
                    body_result = self._cdp_session.send(
                        "Network.getResponseBody", {"requestId": request_id}
                    )
                    body = body_result.get("body", "")
                    if body_result.get("base64Encoded"):
                        # Skip binary payloads
                        body = ""
                    entry["response_body"] = body[:500_000] if body else ""
                except Exception:
                    entry["response_body"] = entry.get("response_body") or ""

            self.network_logs.append(entry)
        except Exception as e:
            logger.debug("CDP loadingFinished handler error: %s", e)

    def _finalize_cdp_pending(self) -> None:
        """Flush CDP entries that never emitted ``loadingFinished``."""
        for request_id, entry in list(self._cdp_pending.items()):
            if entry.get("status") or entry.get("url"):
                self.network_logs.append(entry)
            self._cdp_pending.pop(request_id, None)

    # -------------------------------------------------------- Playwright fallback
    def _handle_response(self, response: Response) -> None:
        """Capture a Playwright response when CDP is unavailable.

        Args:
            response: Completed Playwright response.
        """
        if self._cdp_session is not None:
            # CDP path is primary; skip duplicate Playwright entries
            return
        try:
            req = response.request
            url = response.url
            resource_type = req.resource_type or ""
            if not _should_keep_url(url, resource_type):
                return
            if resource_type in ["stylesheet", "image", "media", "font", "texttrack", "manifest"]:
                return

            post_data = None
            body_type = "empty"
            if req.post_data:
                post_data, body_type = parse_post_data(
                    req.post_data, response.headers.get("content-type", "")
                )

            resp_body = ""
            content_type = response.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type or "javascript" in content_type:
                try:
                    resp_body = response.text()
                except Exception:
                    pass

            req_headers = dict(req.headers)
            req_cookies = []
            cookie_header = req_headers.get("cookie") or req_headers.get("Cookie") or ""
            for part in str(cookie_header).split(";"):
                part = part.strip()
                if "=" in part:
                    n, _, v = part.partition("=")
                    req_cookies.append({"name": n.strip(), "value": v.strip()})

            self.network_logs.append({
                "url": url,
                "method": req.method,
                "headers": req_headers,
                "cookies": req_cookies,
                "post_data": post_data,
                "body_type": body_type,
                "response_headers": dict(response.headers),
                "response_body": resp_body,
                "status": response.status,
                "resource_type": resource_type,
                "initiator_type": None,
                "initiator_url": "",
                "mime_type": content_type,
                "timing": {},
                "step_index": self.current_step_index,
                "step_action": self.current_step_action,
                "capture_source": "playwright",
            })
        except Exception as e:
            logger.warning(f"Failed to record response for {response.url}: {e}")

    # ---------------------------------------------------------------- journey
    def execute_journey(
        self,
        url: str,
        steps: List[Dict[str, Any]],
        clear_context: bool = True,
    ) -> Dict[str, Any]:
        """Execute a user journey and capture browser state and traffic.

        Args:
            url: Initial HTTP(S) URL; may be empty when steps navigate explicitly.
            steps: Action dictionaries accepted by the journey dispatcher.
            clear_context: Whether the caller requests an isolated context. A fresh
                context is currently created for every invocation.

        Returns:
            A dictionary containing network requests, step timeline, cookies, and
            local/session storage. On failure it also contains ``error``,
            ``failed_step``, and ``failed_action``.

        Raises:
            Exception: Browser launch or context creation failures that occur before
                journey error handling begins.
        """
        self.network_logs = []
        self.step_timeline = []
        self._cdp_pending = {}
        cookies: List[Dict[str, Any]] = []
        local_storage: Dict[str, str] = {}
        session_storage: Dict[str, str] = {}

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not self.debug_mode)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()

            self._attach_cdp(page)
            page.on("response", self._handle_response)

            try:
                if url:
                    logger.info(f"Navigating to initial URL: {url}")
                    self.current_step_index = -1
                    self.current_step_action = "initial_navigation"
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(1000)
                    self._ensure_page_document(
                        page,
                        step_index=-1,
                        step_action="initial_navigation",
                        reason="page_url",
                    )
                    self.step_timeline.append({
                        "step_index": -1,
                        "action": "initial_navigation",
                        "selector": "",
                        "value": None,
                        "url_before": url,
                        "url_after": page.url,
                        "sub_task": None,
                    })

                for step_idx, step in enumerate(steps):
                    action = step.get("action")
                    selector = step.get("selector", "")
                    self.current_step_index = step_idx
                    self.current_step_action = action
                    url_before = page.url
                    logger.info(f"Executing step {step_idx + 1}: {action} -> {step}")

                    try:
                        if action == "navigate":
                            page.goto(step["url"], wait_until="networkidle")
                        elif action == "click":
                            page.click(selector, timeout=8000)
                        elif action == "fill":
                            page.fill(selector, step["value"], timeout=8000)
                        elif action == "select":
                            page.select_option(selector, step["value"], timeout=8000)
                        elif action == "wait":
                            page.wait_for_timeout(step.get("timeout", 1000))
                        elif action == "wait_for_selector":
                            page.wait_for_selector(selector, timeout=step.get("timeout", 5000))
                        elif action == "wait_for_load":
                            page.wait_for_load_state("networkidle")
                    except Exception as step_error:
                        if action in ["click", "fill", "select", "wait_for_selector"] and selector:
                            logger.warning(
                                f"Step action '{action}' failed with selector '{selector}': {step_error}. "
                                "Attempting self-healing..."
                            )
                            try:
                                html_content = page.content()[:35000]
                                current_url = page.url
                                # Accessibility tree (same idea as Playwright MCP) for better selector healing
                                try:
                                    snapshot = page.accessibility.snapshot() or {}
                                    a11y_hint = json.dumps(snapshot)[:12000]
                                except Exception:
                                    a11y_hint = "{}"

                                from src.utils.model_router import (
                                    get_model_router,
                                    TaskType,
                                )

                                router = get_model_router()

                                # Inject a11y snapshot into HTML context for the healer
                                heal_context = (
                                    f"{html_content}\n\n"
                                    f"--- Accessibility snapshot (JSON) ---\n{a11y_hint}"
                                )
                                self_heal_prompt = render_prompt(
                                    "browser_self_heal",
                                    action=action,
                                    selector=selector,
                                    current_url=current_url,
                                    action_details=json.dumps(step),
                                    html_content=heal_context,
                                )

                                def _build_heal_chain(llm):
                                    """Return the selected self-healing model unchanged.

                                    Args:
                                        llm: Routed language-model instance.

                                    Returns:
                                        The same model instance.
                                    """
                                    return llm

                                response = router.invoke_with_failover_sync(
                                    TaskType.SELF_HEAL,
                                    _build_heal_chain,
                                    self_heal_prompt,
                                )
                                resp_text = response.content
                                if isinstance(resp_text, list):
                                    resp_text = "".join(str(p) for p in resp_text)
                                if "```" in resp_text:
                                    resp_text = resp_text.split("```json")[-1].split("```")[0].strip()

                                new_selector = json.loads(resp_text).get("selector")
                                if new_selector and new_selector != selector:
                                    logger.info(
                                        f"Self-healed! Retrying action '{action}' with new selector: '{new_selector}'"
                                    )
                                    if action == "click":
                                        page.click(new_selector, timeout=8000)
                                    elif action == "fill":
                                        page.fill(new_selector, step["value"], timeout=8000)
                                    elif action == "select":
                                        page.select_option(new_selector, step["value"], timeout=8000)
                                    elif action == "wait_for_selector":
                                        page.wait_for_selector(
                                            new_selector, timeout=step.get("timeout", 5000)
                                        )
                                else:
                                    raise step_error
                            except Exception as heal_err:
                                logger.error(f"Self-healing extraction failed: {heal_err}")
                                raise step_error
                        else:
                            raise step_error

                    self._settle_after_action(page, action or "")
                    url_after = page.url
                    if url_after != url_before or action in (
                        "navigate", "click", "wait_for_load", "initial_navigation"
                    ):
                        self._ensure_page_document(
                            page,
                            step_index=step_idx,
                            step_action=action or "unknown",
                            reason="page_navigation" if url_after != url_before else "page_url",
                        )
                    self.step_timeline.append({
                        "step_index": step_idx,
                        "action": action,
                        "selector": selector,
                        "value": step.get("value"),
                        "url": step.get("url"),
                        "url_before": url_before,
                        "url_after": url_after,
                        "sub_task": step.get("sub_task"),
                        "cookies": [
                            {"name": c.get("name"), "value": c.get("value"), "domain": c.get("domain")}
                            for c in context.cookies()
                        ],
                    })

                    if self.debug_mode:
                        page.screenshot()

                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1000)
                self._finalize_cdp_pending()

                cookies = context.cookies()
                try:
                    local_storage = json.loads(
                        page.evaluate("() => JSON.stringify(localStorage)")
                    )
                except Exception:
                    local_storage = {}
                try:
                    session_storage = json.loads(
                        page.evaluate("() => JSON.stringify(sessionStorage)")
                    )
                except Exception:
                    session_storage = {}

            except Exception as e:
                logger.error(f"Error during journey execution at step: {e}")
                self._finalize_cdp_pending()
                return {
                    "error": str(e),
                    "failed_step": self.current_step_index,
                    "failed_action": self.current_step_action,
                    "network_requests": self.network_logs,
                    "step_timeline": self.step_timeline,
                    "cookies": cookies,
                    "local_storage": local_storage,
                    "session_storage": session_storage,
                }
            finally:
                try:
                    if self._cdp_session:
                        self._cdp_session.detach()
                except Exception:
                    pass
                context.close()
                browser.close()

        return {
            "network_requests": self.network_logs,
            "step_timeline": self.step_timeline,
            "cookies": cookies,
            "local_storage": local_storage,
            "session_storage": session_storage,
        }
