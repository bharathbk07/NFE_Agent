"""
Browser journey recorder using Playwright + Chrome DevTools Protocol (CDP).

NOTE: Chrome DevTools *MCP* (chrome-devtools-mcp) is an IDE/agent bridge for
interactive debugging in Cursor. It is not suitable as the capture layer for
this LangGraph pipeline (needs dual deterministic runs, step tagging, and
headless automation). Playwright already speaks CDP; we use CDP Network.*
events here to get DevTools-grade request/response detail for:
  - parameterization
  - correlation (extract → pass)
  - transaction grouping
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, Page, Response, Request

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
    def __init__(self, debug_mode: bool = False):
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
        """
        Record the current page URL as a Document GET when CDP missed a soft/hard
        navigation (common for SPAs / client-side redirects after login).
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
        """Give navigations / XHR a moment to fire and attribute to the current step."""
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
        """Enable Chrome DevTools Protocol Network domain for rich capture."""
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
        try:
            request_id = params.get("requestId")
            req = params.get("request") or {}
            url = req.get("url", "")
            resource_type = params.get("type") or ""
            if not request_id or not _should_keep_url(url, resource_type):
                return

            post_data = req.get("postData")
            parsed_post: Any = post_data
            if isinstance(post_data, str) and post_data:
                try:
                    parsed_post = json.loads(post_data)
                except Exception:
                    parsed_post = post_data

            initiator = params.get("initiator") or {}
            self._cdp_pending[request_id] = {
                "request_id": request_id,
                "url": url,
                "method": req.get("method", "GET"),
                "headers": req.get("headers") or {},
                "cookies": [],
                "post_data": parsed_post,
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
            # Refresh step tagging to current action when response arrives
            entry["step_index"] = self.current_step_index
            entry["step_action"] = self.current_step_action
        except Exception as e:
            logger.debug("CDP responseReceived handler error: %s", e)

    def _on_cdp_loading_finished(self, params: Dict[str, Any]) -> None:
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
        """Flush any pending CDP entries that never got loadingFinished."""
        for request_id, entry in list(self._cdp_pending.items()):
            if entry.get("status") or entry.get("url"):
                self.network_logs.append(entry)
            self._cdp_pending.pop(request_id, None)

    # -------------------------------------------------------- Playwright fallback
    def _handle_response(self, response: Response) -> None:
        """Fallback when CDP is unavailable; also fills gaps for navigations."""
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
            if req.post_data:
                post_data = req.post_data
                try:
                    post_data = json.loads(post_data)
                except Exception:
                    pass

            resp_body = ""
            content_type = response.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type or "javascript" in content_type:
                try:
                    resp_body = response.text()
                except Exception:
                    pass

            self.network_logs.append({
                "url": url,
                "method": req.method,
                "headers": dict(req.headers),
                "cookies": [],
                "post_data": post_data,
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
        """
        Executes a user journey and captures network traffic via CDP (+ Playwright fallback).
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
                                    invoke_llm_sync,
                                )
                                import os

                                llm = get_model_router().get_llm(TaskType.SELF_HEAL)
                                current_dir = os.path.dirname(os.path.abspath(__file__))
                                prompt_path = os.path.abspath(
                                    os.path.join(current_dir, "..", "..", "prompts", "browser_self_heal.txt")
                                )
                                with open(prompt_path, "r", encoding="utf-8") as f:
                                    prompt_template = f.read()

                                # Inject a11y snapshot into HTML context for the healer
                                heal_context = (
                                    f"{html_content}\n\n"
                                    f"--- Accessibility snapshot (JSON) ---\n{a11y_hint}"
                                )
                                self_heal_prompt = prompt_template.format(
                                    action=action,
                                    selector=selector,
                                    current_url=current_url,
                                    action_details=json.dumps(step),
                                    html_content=heal_context,
                                )
                                response = invoke_llm_sync(llm, self_heal_prompt)
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
