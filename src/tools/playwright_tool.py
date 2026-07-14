import logging
import json
import time
from typing import List, Dict, Any, Tuple
from playwright.sync_api import sync_playwright, Page, Response, Request

logger = logging.getLogger(__name__)

class PlaywrightBrowserRecorder:
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.network_logs: List[Dict[str, Any]] = []

    def _handle_request(self, request: Request):
        pass

    def _handle_response(self, response: Response):
        try:
            # We filter out static assets to keep data size reasonable and relevant to API traffic
            url = response.url
            if any(ext in url.lower() for ext in [".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".woff", ".svg", ".ico"]):
                return

            req = response.request
            
            # Read post data if any
            post_data = None
            if req.post_data:
                post_data = req.post_data
                try:
                    post_data = json.loads(post_data)
                except Exception:
                    pass

            # Read response body if available and text-based
            resp_body = ""
            content_type = response.headers.get("content-type", "")
            if "text" in content_type or "json" in content_type or "javascript" in content_type:
                try:
                    resp_body = response.text()
                except Exception:
                    pass

            log_entry = {
                "url": url,
                "method": req.method,
                "headers": dict(req.headers),
                "cookies": [],  # will be extracted separately via context
                "post_data": post_data,
                "response_headers": dict(response.headers),
                "response_body": resp_body,
                "status": response.status
            }
            self.network_logs.append(log_entry)
        except Exception as e:
            logger.warning(f"Failed to record response for {response.url}: {e}")

    def execute_journey(
        self,
        url: str,
        steps: List[Dict[str, Any]],
        clear_context: bool = True
    ) -> Dict[str, Any]:
        """
        Executes a user journey and captures all network traffic.
        Steps format example:
        [
            {"action": "navigate", "url": "https://example.com"},
            {"action": "fill", "selector": "#username", "value": "user"},
            {"action": "fill", "selector": "#password", "value": "pass"},
            {"action": "click", "selector": "button[type='submit']"},
            {"action": "wait_for_load"}
        ]
        """
        self.network_logs = []
        screenshots = []
        cookies = []
        local_storage = {}
        session_storage = {}

        with sync_playwright() as p:
            # Run headless for stability unless debug mode is enabled
            browser = p.chromium.launch(headless=not self.debug_mode)
            context = browser.new_context(viewport={"width": 1280, "height": 720})

            # Clear context if requested (Playwright's new_context starts fresh by default)
            page = context.new_page()
            
            # Listen to network responses
            page.on("response", self._handle_response)

            try:
                # Initial navigation if url provided
                if url:
                    logger.info(f"Navigating to initial URL: {url}")
                    page.goto(url, wait_until="networkidle")
                    page.wait_for_timeout(1000)

                for step_idx, step in enumerate(steps):
                    action = step.get("action")
                    selector = step.get("selector", "")
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
                        if action in ["click", "fill", "select"] and selector:
                            logger.warning(f"Step action '{action}' failed with selector '{selector}': {step_error}. Attempting self-healing...")
                            try:
                                html_content = page.content()[:45000] # Limit size to fit within token boundaries
                                current_url = page.url
                                
                                from langchain_google_genai import ChatGoogleGenerativeAI
                                from config.settings import settings
                                
                                llm = ChatGoogleGenerativeAI(
                                    model=settings.GEMINI_MODEL,
                                    google_api_key=settings.GEMINI_API_KEY,
                                    temperature=0.1
                                )
                                
                                self_heal_prompt = (
                                    "You are an expert browser automation self-healing agent.\n"
                                    f"We attempted to perform the action '{action}' on the selector '{selector}', but it failed/timed out.\n"
                                    f"Current Page URL: {current_url}\n\n"
                                    "Here is the HTML content of the current page:\n"
                                    f"```html\n{html_content}\n```\n\n"
                                    f"Based on the page content, find the correct CSS selector for the element we are trying to interact with. "
                                    f"The action details are: {step}\n"
                                    "Return a JSON object with the key 'selector' containing the correct CSS selector to use. "
                                    "Do not return any markdown or backticks."
                                )
                                
                                response = llm.invoke(self_heal_prompt)
                                resp_text = response.content
                                if isinstance(resp_text, list):
                                    resp_text = "".join(str(p) for p in resp_text)
                                if "```" in resp_text:
                                    resp_text = resp_text.split("```json")[-1].split("```")[0].strip()
                                
                                new_selector = json.loads(resp_text).get("selector")
                                if new_selector and new_selector != selector:
                                    logger.info(f"Self-healed! Retrying action '{action}' with new selector: '{new_selector}'")
                                    if action == "click":
                                        page.click(new_selector, timeout=8000)
                                    elif action == "fill":
                                        page.fill(new_selector, step["value"], timeout=8000)
                                    elif action == "select":
                                        page.select_option(new_selector, step["value"], timeout=8000)
                                    continue
                            except Exception as heal_err:
                                logger.error(f"Self-healing extraction failed: {heal_err}")
                        
                        # Re-raise original error if self-healing could not resolve
                        raise step_error
                    
                    # Capture optional screenshot after specific steps or each step
                    if self.debug_mode:
                        screenshot_bytes = page.screenshot()
                        # We can store/handle screenshots if needed
                    
                    # Small stabilization wait
                    page.wait_for_timeout(500)

                # Wait for any lingering network requests to settle
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1000)

                # Capture final session / storage state
                cookies = context.cookies()
                
                # Extract storage state
                try:
                    local_storage = page.evaluate("() => JSON.stringify(localStorage)")
                    local_storage = json.loads(local_storage)
                except Exception:
                    local_storage = {}

                try:
                    session_storage = page.evaluate("() => JSON.stringify(sessionStorage)")
                    session_storage = json.loads(session_storage)
                except Exception:
                    session_storage = {}

            except Exception as e:
                logger.error(f"Error during journey execution at step: {e}")
                raise e
            finally:
                context.close()
                browser.close()

        return {
            "network_requests": self.network_logs,
            "cookies": cookies,
            "local_storage": local_storage,
            "session_storage": session_storage
        }
