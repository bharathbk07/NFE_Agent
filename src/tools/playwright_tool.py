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

    # -------------------------------------------------------- Watch-me recording
    _WATCH_ME_INIT_JS = r"""
(() => {
  if (window.__nfeWatchMeInstalled) return;
  window.__nfeWatchMeInstalled = true;

  function loadTxnName() {
    try { return sessionStorage.getItem('nfeWatchMeTxn') || ''; } catch (e) { return ''; }
  }
  function saveTxnName(name) {
    try {
      if (name) sessionStorage.setItem('nfeWatchMeTxn', name);
      else sessionStorage.removeItem('nfeWatchMeTxn');
    } catch (e) {}
  }
  function loadPaused() {
    try { return sessionStorage.getItem('nfeWatchMePaused') === '1'; } catch (e) { return false; }
  }
  function savePaused(paused) {
    try { sessionStorage.setItem('nfeWatchMePaused', paused ? '1' : '0'); } catch (e) {}
  }
  function clearWatchMeSession() {
    try {
      sessionStorage.removeItem('nfeWatchMeTxn');
      sessionStorage.removeItem('nfeWatchMePaused');
    } catch (e) {}
  }

  // Survive full page navigations / refreshes (same-origin)
  window.__nfeCurrentTxn = loadTxnName();
  window.__nfeRecordingPaused = loadPaused();

  function setCurrentTxn(name) {
    window.__nfeCurrentTxn = name || '';
    saveTxnName(window.__nfeCurrentTxn);
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(String(value));
    return String(value).replace(/([ !"#$%&'()*+,./:;<=>?@[\\\]^`{|}~])/g, '\\$1');
  }

  function stableSelector(el) {
    if (!el || el.nodeType !== 1) return '';
    if (el.closest && el.closest('#nfe-watch-me-overlay')) return '';

    const testId = el.getAttribute && el.getAttribute('data-testid');
    if (testId) return `[data-testid="${cssEscape(testId)}"]`;

    if (el.id && !/^[0-9]/.test(el.id) && !/^(ember|react|vue|ng)/i.test(el.id)) {
      return `#${cssEscape(el.id)}`;
    }

    const name = el.getAttribute && el.getAttribute('name');
    if (name) {
      const tag = el.tagName.toLowerCase();
      const type = el.getAttribute('type');
      if (type) return `${tag}[name="${cssEscape(name)}"][type="${cssEscape(type)}"]`;
      return `${tag}[name="${cssEscape(name)}"]`;
    }

    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) {
      return `${el.tagName.toLowerCase()}[aria-label="${cssEscape(aria)}"]`;
    }

    const parts = [];
    let node = el;
    let depth = 0;
    while (node && node.nodeType === 1 && depth < 5 && node !== document.body) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        parts.unshift(`#${cssEscape(node.id)}`);
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (siblings.length > 1) {
          const idx = siblings.indexOf(node) + 1;
          part += `:nth-of-type(${idx})`;
        }
      }
      parts.unshift(part);
      node = parent;
      depth += 1;
    }
    return parts.join(' > ');
  }

  function emit(step) {
    if (window.__nfeRecordingPaused) return;
    if (!step || !step.action) return;
    const action = step.action;
    if (
      action !== 'navigate' &&
      action !== 'txn_start' &&
      action !== 'txn_end' &&
      !step.selector
    ) {
      return;
    }
    if (!window.__nfeCurrentTxn) {
      window.__nfeCurrentTxn = loadTxnName();
    }
    if (window.__nfeCurrentTxn && !step.sub_task) {
      step.sub_task = window.__nfeCurrentTxn;
    }
    try {
      window.nfeRecordStep(step);
    } catch (e) {}
  }

  function updateOverlayUi() {
    const banner = document.getElementById('nfe-watch-me-handle');
    const status = document.getElementById('nfe-txn-status');
    const endTxnBtn = document.getElementById('nfe-end-txn');
    const pauseBtn = document.getElementById('nfe-pause-recording');
    const txn = window.__nfeCurrentTxn || loadTxnName();
    window.__nfeCurrentTxn = txn;

    if (banner) {
      if (window.__nfeRecordingPaused) {
        banner.textContent = '⏸ PAUSED — drag to move';
        banner.style.background = '#1e3a5f';
      } else if (txn) {
        banner.textContent = '● ACTIVE TXN: ' + txn + ' — drag to move';
        banner.style.background = '#14532d';
      } else {
        banner.textContent = 'NFE Watch-me — Start a TXN, then click the app — drag to move';
        banner.style.background = '#111827';
      }
    }
    if (status) {
      if (txn) {
        status.textContent = 'Recording into "' + txn + '" (keeps name across page refresh)';
        status.style.background = '#166534';
        status.style.color = '#ecfdf5';
        status.style.display = 'block';
      } else {
        status.textContent = 'No active TXN — type a name and press Start TXN';
        status.style.background = '#374151';
        status.style.color = '#e5e7eb';
        status.style.display = 'block';
      }
    }
    if (endTxnBtn) {
      endTxnBtn.disabled = !txn;
      endTxnBtn.style.opacity = txn ? '1' : '0.5';
      endTxnBtn.textContent = txn ? ('End "' + txn + '"') : 'End TXN';
    }
    if (pauseBtn) {
      pauseBtn.textContent = window.__nfeRecordingPaused ? 'Resume' : 'Pause';
      pauseBtn.style.background = window.__nfeRecordingPaused ? '#2563eb' : '#d97706';
    }
  }

  document.addEventListener(
    'click',
    (event) => {
      const t = event.target;
      if (!t || !t.closest) return;
      if (t.closest('#nfe-watch-me-overlay')) return;
      const el = t.closest('a, button, input, select, textarea, [role="button"], [onclick]') || t;
      const tag = (el.tagName || '').toLowerCase();
      const type = ((el.getAttribute && el.getAttribute('type')) || '').toLowerCase();
      if (tag === 'textarea') return;
      if (tag === 'select') return;
      if (tag === 'input' && !['button', 'submit', 'checkbox', 'radio', 'image', 'reset'].includes(type)) {
        return;
      }
      const selector = stableSelector(el);
      if (!selector) return;
      emit({ action: 'click', selector });
    },
    true
  );

  document.addEventListener(
    'change',
    (event) => {
      const el = event.target;
      if (!el || !el.closest || el.closest('#nfe-watch-me-overlay')) return;
      const selector = stableSelector(el);
      if (!selector) return;
      const tag = (el.tagName || '').toLowerCase();
      if (tag === 'select') {
        emit({ action: 'select', selector, value: el.value || '' });
        return;
      }
      if (tag === 'input' || tag === 'textarea') {
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (['checkbox', 'radio', 'button', 'submit', 'file', 'image'].includes(type)) {
          return;
        }
        emit({ action: 'fill', selector, value: el.value || '' });
      }
    },
    true
  );

  function loadPanelPos() {
    try {
      const raw = sessionStorage.getItem('nfeWatchMePanelPos');
      if (!raw) return null;
      const pos = JSON.parse(raw);
      if (typeof pos.left === 'number' && typeof pos.top === 'number') return pos;
    } catch (e) {}
    return null;
  }

  function savePanelPos(left, top) {
    try {
      sessionStorage.setItem('nfeWatchMePanelPos', JSON.stringify({ left, top }));
    } catch (e) {}
  }

  function makeDraggable(root, handle) {
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let origLeft = 0;
    let origTop = 0;

    const onMove = (e) => {
      if (!dragging) return;
      const dx = e.clientX - startX;
      const dy = e.clientY - startY;
      let left = origLeft + dx;
      let top = origTop + dy;
      const maxL = Math.max(0, window.innerWidth - root.offsetWidth);
      const maxT = Math.max(0, window.innerHeight - root.offsetHeight);
      left = Math.min(Math.max(0, left), maxL);
      top = Math.min(Math.max(0, top), maxT);
      root.style.left = left + 'px';
      root.style.top = top + 'px';
      root.style.right = 'auto';
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('mouseup', onUp, true);
      savePanelPos(parseFloat(root.style.left) || 0, parseFloat(root.style.top) || 0);
    };

    handle.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest('button, input')) return;
      e.preventDefault();
      e.stopPropagation();
      dragging = true;
      const rect = root.getBoundingClientRect();
      startX = e.clientX;
      startY = e.clientY;
      origLeft = rect.left;
      origTop = rect.top;
      root.style.left = origLeft + 'px';
      root.style.top = origTop + 'px';
      root.style.right = 'auto';
      document.addEventListener('mousemove', onMove, true);
      document.addEventListener('mouseup', onUp, true);
    });
  }

  function btnStyle(bg) {
    return [
      'pointer-events:auto', 'cursor:pointer', 'background:' + bg, 'color:#fff',
      'border:none', 'border-radius:8px', 'padding:8px 12px', 'font-size:13px',
      'font-weight:600', 'box-shadow:0 2px 8px rgba(0,0,0,.25)',
    ].join(';');
  }

  window.__nfeRefreshOverlay = () => {
    const stored = loadTxnName();
    if (stored) window.__nfeCurrentTxn = stored;
    window.__nfeRecordingPaused = loadPaused();
    updateOverlayUi();
  };

  window.__nfeInjectOverlay = () => {
    const existing = document.getElementById('nfe-watch-me-overlay');
    if (existing) {
      window.__nfeRefreshOverlay();
      return;
    }
    window.__nfeCurrentTxn = loadTxnName() || window.__nfeCurrentTxn || '';
    window.__nfeRecordingPaused = loadPaused();

    const root = document.createElement('div');
    root.id = 'nfe-watch-me-overlay';
    root.setAttribute('data-nfe-overlay', '1');
    const saved = loadPanelPos();
    const left = saved ? saved.left + 'px' : 'auto';
    const top = saved ? saved.top + 'px' : '12px';
    const right = saved ? 'auto' : '12px';
    root.style.cssText = [
      'position:fixed', 'z-index:2147483647', 'top:' + top, 'right:' + right,
      'left:' + left, 'font-family:system-ui,sans-serif', 'display:flex',
      'flex-direction:column', 'gap:6px', 'align-items:stretch',
      'pointer-events:auto', 'min-width:280px', 'max-width:360px', 'user-select:none',
    ].join(';');

    const banner = document.createElement('div');
    banner.id = 'nfe-watch-me-handle';
    banner.style.cssText = [
      'background:#111827', 'color:#f9fafb', 'padding:10px 12px',
      'border-radius:8px', 'font-size:13px', 'font-weight:600',
      'box-shadow:0 4px 16px rgba(0,0,0,.25)',
      'cursor:move', 'pointer-events:auto', 'line-height:1.35',
    ].join(';');

    const status = document.createElement('div');
    status.id = 'nfe-txn-status';
    status.style.cssText = [
      'padding:8px 10px', 'border-radius:8px', 'font-size:12px', 'font-weight:600',
      'box-shadow:0 2px 8px rgba(0,0,0,.2)', 'pointer-events:none',
    ].join(';');

    const txnRow = document.createElement('div');
    txnRow.style.cssText = 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;';

    const txnInput = document.createElement('input');
    txnInput.type = 'text';
    txnInput.id = 'nfe-txn-name';
    txnInput.placeholder = 'TXN name (e.g. Login)';
    txnInput.style.cssText = [
      'flex:1', 'min-width:120px', 'padding:8px 10px', 'border-radius:8px',
      'border:1px solid #374151', 'background:#1f2937', 'color:#f9fafb',
      'font-size:13px', 'pointer-events:auto',
    ].join(';');
    txnInput.addEventListener('mousedown', (e) => e.stopPropagation());
    txnInput.addEventListener('click', (e) => e.stopPropagation());
    txnInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        startTxnBtn.click();
      }
    });

    const startTxnBtn = document.createElement('button');
    startTxnBtn.type = 'button';
    startTxnBtn.id = 'nfe-start-txn';
    startTxnBtn.textContent = 'Start TXN';
    startTxnBtn.style.cssText = btnStyle('#7c3aed');
    startTxnBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const name = (txnInput.value || '').trim() || 'Transaction';
      if (window.__nfeCurrentTxn) {
        emit({ action: 'txn_end', value: window.__nfeCurrentTxn, sub_task: window.__nfeCurrentTxn });
      }
      setCurrentTxn(name);
      emit({ action: 'txn_start', value: name, sub_task: name });
      txnInput.value = '';
      updateOverlayUi();
    });

    const endTxnBtn = document.createElement('button');
    endTxnBtn.type = 'button';
    endTxnBtn.id = 'nfe-end-txn';
    endTxnBtn.textContent = 'End TXN';
    endTxnBtn.style.cssText = btnStyle('#4b5563');
    endTxnBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (!window.__nfeCurrentTxn) return;
      const name = window.__nfeCurrentTxn;
      emit({ action: 'txn_end', value: name, sub_task: name });
      setCurrentTxn('');
      updateOverlayUi();
    });

    txnRow.appendChild(txnInput);
    txnRow.appendChild(startTxnBtn);
    txnRow.appendChild(endTxnBtn);

    const row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:6px;justify-content:flex-end;flex-wrap:wrap;';

    const pauseBtn = document.createElement('button');
    pauseBtn.type = 'button';
    pauseBtn.id = 'nfe-pause-recording';
    pauseBtn.textContent = 'Pause';
    pauseBtn.style.cssText = btnStyle('#d97706');
    pauseBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      window.__nfeRecordingPaused = !window.__nfeRecordingPaused;
      savePaused(window.__nfeRecordingPaused);
      updateOverlayUi();
      try { window.nfeSetPaused(window.__nfeRecordingPaused); } catch (err) {}
    });

    const doneBtn = document.createElement('button');
    doneBtn.type = 'button';
    doneBtn.id = 'nfe-done-recording';
    doneBtn.textContent = 'Done';
    doneBtn.style.cssText = btnStyle('#059669');
    doneBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (window.__nfeCurrentTxn) {
        emit({ action: 'txn_end', value: window.__nfeCurrentTxn, sub_task: window.__nfeCurrentTxn });
        setCurrentTxn('');
      }
      clearWatchMeSession();
      try { window.nfeDoneRecording(); } catch (err) {}
    });

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.id = 'nfe-cancel-recording';
    cancelBtn.textContent = 'Cancel';
    cancelBtn.style.cssText = btnStyle('#dc2626');
    cancelBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      clearWatchMeSession();
      try { window.nfeCancelRecording(); } catch (err) {}
    });

    row.appendChild(pauseBtn);
    row.appendChild(doneBtn);
    row.appendChild(cancelBtn);
    root.appendChild(banner);
    root.appendChild(status);
    root.appendChild(txnRow);
    root.appendChild(row);
    (document.body || document.documentElement).appendChild(root);
    makeDraggable(root, banner);
    updateOverlayUi();
  };

  // Python can re-push TXN after cross-origin navigations where sessionStorage is empty
  window.__nfeApplyRecorderState = (name, paused) => {
    if (typeof name === 'string') {
      setCurrentTxn(name);
    }
    if (typeof paused === 'boolean') {
      window.__nfeRecordingPaused = paused;
      savePaused(paused);
    }
    try { window.__nfeInjectOverlay(); } catch (e) {}
    try { window.__nfeRefreshOverlay(); } catch (e) {}
  };

  const boot = () => {
    try { window.__nfeInjectOverlay(); } catch (e) {}
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
"""

    def record_watch_me(
        self,
        url: str,
        *,
        timeout_ms: int = 15 * 60 * 1000,
    ) -> Dict[str, Any]:
        """Open a headed browser, record user interactions, and capture CDP traffic.

        The user drives the journey in the visible window. The overlay supports
        **Start TXN / End TXN** (TXN name survives refresh/navigation),
        **Done**, **Pause/Resume**, **Cancel**, and
        drag-to-move. Captured steps are suitable for :meth:`execute_journey` replay.

        Args:
            url: Starting URL for the headed Chromium session.
            timeout_ms: Maximum wait for Done/Cancel before aborting (default 15 minutes).

        Returns:
            Same shape as :meth:`execute_journey`, plus ``recorded_steps`` for replay.
            On timeout, cancel, or failure, includes ``error`` and optional
            ``cancelled`` / ``status`` flags.
        """
        self.network_logs = []
        self.step_timeline = []
        self._cdp_pending = {}
        self.current_step_index = -1
        self.current_step_action = "initial_navigation"

        recorded_steps: List[Dict[str, Any]] = []
        session = {"done": False, "cancelled": False, "paused": False}
        current_txn: Dict[str, Optional[str]] = {"name": None}
        cookies: List[Dict[str, Any]] = []
        local_storage: Dict[str, str] = {}
        session_storage: Dict[str, str] = {}
        last_url = {"value": ""}

        def _append_step(step: Dict[str, Any]) -> None:
            """Store a normalized Playwright step and timeline entry."""
            if session["paused"] or session["cancelled"] or session["done"]:
                return
            action = step.get("action")
            if not action:
                return

            if action == "txn_start":
                name = str(step.get("value") or step.get("sub_task") or "Transaction").strip()
                current_txn["name"] = name or "Transaction"
                sub_task = current_txn["name"]
            elif action == "txn_end":
                name = str(
                    step.get("value")
                    or step.get("sub_task")
                    or current_txn["name"]
                    or "Transaction"
                ).strip()
                sub_task = name or "Transaction"
            else:
                sub_task = str(
                    step.get("sub_task")
                    or current_txn["name"]
                    or "watch_me_flow"
                ).strip() or "watch_me_flow"

            normalized: Dict[str, Any] = {
                "action": action,
                "sub_task": sub_task,
            }
            if step.get("selector"):
                normalized["selector"] = step["selector"]
            if "value" in step and step["value"] is not None:
                normalized["value"] = step["value"]
            if step.get("url"):
                normalized["url"] = step["url"]

            step_idx = len(recorded_steps)
            recorded_steps.append(normalized)
            self.current_step_index = step_idx
            self.current_step_action = action

            if action == "txn_end":
                current_txn["name"] = None

            log_value = normalized.get("value")
            sel = str(normalized.get("selector") or "")
            # Keep real value in recorded_steps for replay; mask timeline only.
            if log_value is not None and "password" in sel.lower():
                log_value = "***"

            self.step_timeline.append({
                "step_index": step_idx,
                "action": action,
                "selector": normalized.get("selector", ""),
                "value": log_value,
                "url": normalized.get("url"),
                "url_before": last_url["value"],
                "url_after": last_url["value"],
                "sub_task": sub_task,
            })
            logger.info("Watch-me recorded step %s: %s", step_idx + 1, normalized)

        def _result_payload(*, error: Optional[str] = None) -> Dict[str, Any]:
            """Build the standard Watch-me return dictionary."""
            payload: Dict[str, Any] = {
                "recorded_steps": recorded_steps,
                "network_requests": self.network_logs,
                "step_timeline": self.step_timeline,
                "cookies": cookies,
                "local_storage": local_storage,
                "session_storage": session_storage,
            }
            if session["cancelled"]:
                payload["cancelled"] = True
                payload["status"] = "cancelled"
                payload["error"] = error or "Watch-me recording cancelled by user"
            elif error:
                payload["error"] = error
            return payload

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(viewport={"width": 1280, "height": 720})
            page = context.new_page()

            def on_record_step(source: Any, step: Any) -> None:
                """Receive a DOM interaction from the page binding."""
                if not isinstance(step, dict):
                    return
                _append_step(step)

            def on_done_recording(source: Any) -> None:
                """Unblock the recording wait when the user clicks Done."""
                session["done"] = True
                logger.info("Watch-me Done recording clicked.")

            def on_cancel_recording(source: Any) -> None:
                """Abort recording without continuing to analysis."""
                session["cancelled"] = True
                session["done"] = True
                logger.info("Watch-me Cancel clicked.")

            def on_set_paused(source: Any, paused: Any) -> None:
                """Mirror the overlay pause state in Python."""
                session["paused"] = bool(paused)
                logger.info("Watch-me paused=%s", session["paused"])

            page.expose_binding("nfeRecordStep", on_record_step)
            page.expose_binding("nfeDoneRecording", on_done_recording)
            page.expose_binding("nfeCancelRecording", on_cancel_recording)
            page.expose_binding("nfeSetPaused", on_set_paused)
            page.add_init_script(self._WATCH_ME_INIT_JS)

            self._attach_cdp(page)
            page.on("response", self._handle_response)

            def _sync_overlay_state() -> None:
                """Re-inject overlay and restore active TXN after navigations/refreshes.

                sessionStorage covers same-origin reloads; Python also pushes
                ``current_txn`` so cross-origin hops keep the name visible.
                """
                try:
                    page.evaluate(
                        """([name, paused]) => {
                          if (window.__nfeApplyRecorderState) {
                            window.__nfeApplyRecorderState(name || '', !!paused);
                            return;
                          }
                          if (window.__nfeInjectOverlay) window.__nfeInjectOverlay();
                        }""",
                        [current_txn.get("name") or "", session.get("paused", False)],
                    )
                except Exception:
                    pass

            def _on_frame_navigated(frame: Any) -> None:
                """Record top-frame navigations after the initial load."""
                try:
                    if frame != page.main_frame:
                        return
                    new_url = frame.url or ""
                    if not new_url.startswith("http"):
                        return
                    if new_url == last_url["value"]:
                        return
                    prev = last_url["value"]
                    last_url["value"] = new_url
                    # Skip synthetic navigate for the very first landing URL.
                    if prev and prev.startswith("http") and new_url != prev:
                        _append_step({"action": "navigate", "url": new_url})
                        if self.step_timeline:
                            self.step_timeline[-1]["url_before"] = prev
                            self.step_timeline[-1]["url_after"] = new_url
                    _sync_overlay_state()
                except Exception as nav_err:
                    logger.debug("Watch-me navigation hook error: %s", nav_err)

            page.on("framenavigated", _on_frame_navigated)

            try:
                logger.info("Watch-me: opening headed browser at %s", url)
                self.current_step_index = -1
                self.current_step_action = "initial_navigation"
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(800)
                last_url["value"] = page.url
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
                    "sub_task": "watch_me_flow",
                })
                _sync_overlay_state()

                deadline = time.time() + (timeout_ms / 1000.0)
                while not session["done"] and time.time() < deadline:
                    page.wait_for_timeout(400)
                    # Keep TXN name visible if SPA wiped the overlay mid-session.
                    _sync_overlay_state()

                if session["cancelled"]:
                    self._finalize_cdp_pending()
                    return _result_payload()

                if not session["done"]:
                    self._finalize_cdp_pending()
                    return _result_payload(
                        error=(
                            f"Watch-me timed out after {timeout_ms // 1000}s "
                            "waiting for Done recording"
                        )
                    )

                page.wait_for_timeout(1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
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
                logger.error("Watch-me recording failed: %s", e)
                self._finalize_cdp_pending()
                return _result_payload(error=str(e))
            finally:
                try:
                    if self._cdp_session:
                        self._cdp_session.detach()
                except Exception:
                    pass
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        return {
            "network_requests": self.network_logs,
            "step_timeline": self.step_timeline,
            "cookies": cookies,
            "local_storage": local_storage,
            "session_storage": session_storage,
            "recorded_steps": recorded_steps,
            "status": "completed",
        }
