import json
import logging
import re
from typing import List, Dict, Any
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


class ParameterAgent:
    """Identifies user inputs that should be parameterized in performance test scripts."""

    def analyze(
        self,
        user_steps: List[Any],
        run1_requests: List[Dict[str, Any]],
        credentials: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Map fill/select step values to network request locations."""
        parameterizable_candidates = []
        creds_values = {
            str(val).lower(): key for key, val in credentials.items() if val
        }

        for step in user_steps:
            if not isinstance(step, dict) or step.get("action") not in ("fill", "select"):
                continue

            val = str(step.get("value", ""))
            selector = step.get("selector", "")
            if not val or not selector:
                continue

            val_lower = val.lower()
            is_credential = False
            cred_name = None
            for cred_val, cred_key in creds_values.items():
                if cred_val == val_lower or val_lower in cred_val or cred_val in val_lower:
                    is_credential = True
                    cred_name = cred_key
                    break

            propagations = self._find_propagations(val, val_lower, run1_requests)

            variable_name = cred_name if is_credential else self._suggest_variable_name(selector, val)

            parameterizable_candidates.append({
                "selector": selector,
                "value": val,
                "variable_name": variable_name,
                "is_credential": is_credential,
                "credential_name": cred_name,
                "propagations": list(set(propagations)),
            })

        return parameterizable_candidates

    def _suggest_variable_name(self, selector: str, value: str) -> str:
        """Derive a load-test variable name from the selector or value."""
        for pattern in [
            r'name=["\']?(\w+)',
            r'id=["\']?(\w+)',
            r'placeholder\*?=["\']?(\w+)',
            r'#(\w+)',
        ]:
            match = re.search(pattern, selector, re.IGNORECASE)
            if match:
                return match.group(1).lower()
        return re.sub(r"[^a-zA-Z0-9_]", "_", value[:20].lower()).strip("_") or "input_value"

    def _find_propagations(
        self, val: str, val_lower: str, requests: List[Dict[str, Any]]
    ) -> List[str]:
        propagations = []
        for req in requests:
            req_url = req.get("url", "")
            req_method = req.get("method", "")

            try:
                parsed = urlparse(req_url)
                for q_key, q_vals in parse_qs(parsed.query).items():
                    if any(self._matches_value(val_lower, qv) for qv in q_vals):
                        propagations.append(
                            f"`{req_method}` Query Parameter `{q_key}` in `{req_url}`"
                        )
            except Exception:
                pass

            post_data = req.get("post_data")
            if post_data:
                try:
                    post_str = json.dumps(post_data) if isinstance(post_data, (dict, list)) else str(post_data)
                    if self._matches_value(val_lower, post_str):
                        propagations.append(f"`{req_method}` Post Body in `{req_url}`")
                except Exception:
                    pass

            skip_headers = {
                "host", "referer", "user-agent", "accept-encoding",
                "accept-language", "connection", "accept",
            }
            for h_key, h_val in req.get("headers", {}).items():
                if h_key.lower() not in skip_headers and self._matches_value(val_lower, h_val):
                    propagations.append(
                        f"`{req_method}` Header `{h_key}` in `{req_url}`"
                    )

        return propagations

    @staticmethod
    def _matches_value(val_lower: str, target_text: str) -> bool:
        if not target_text:
            return False
        target_lower = str(target_text).lower()
        if len(val_lower) <= 2:
            return bool(re.search(rf"\b{re.escape(val_lower)}\b", target_lower))
        return val_lower in target_lower
