"""Detects user inputs to parameterize in generated NFE load-test scripts."""

import json
import logging
import re
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse, parse_qs

from src.utils.http_body import (
    content_type_from_headers,
    flatten_body_fields,
    parse_post_data,
)
from src.utils.correlation_noise import (
    is_login_field_selector,
    is_static_asset_url,
)
from src.utils.perf_test_classification import (
    is_correlation_field_selector,
    is_placeholder_value,
    should_treat_fill_as_correlation,
    suggest_correlation_var_name,
)

logger = logging.getLogger(__name__)


class ParameterAgent:
    """Identifies user inputs that should be parameterized in performance test scripts."""

    def analyze(
        self,
        user_steps: List[Any],
        run1_requests: List[Dict[str, Any]],
        credentials: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        """Map UI input values to their network propagation locations.

        Args:
            user_steps: Ordered browser steps containing fill/select actions.
            run1_requests: Network requests captured during the first run.
            credentials: Credential values keyed by logical variable name.

        Returns:
            Parameter candidate dictionaries with names and propagation sites.
        """
        parameterizable_candidates = []
        creds_values = {
            str(val).lower(): key for key, val in credentials.items() if val
        }

        for step_idx, step in enumerate(user_steps):
            if not isinstance(step, dict) or step.get("action") not in ("fill", "select"):
                continue

            val = str(step.get("value", ""))
            selector = step.get("selector", "")
            if not val or not selector:
                continue

            if is_placeholder_value(val):
                continue

            # Values sourced from an earlier response must be extracted at
            # runtime rather than supplied as static test-data parameters.
            if should_treat_fill_as_correlation(
                step, step_idx, user_steps, run1_requests, credentials
            ):
                logger.debug(
                    "Skipping correlation field (not a parameter): %s = %r",
                    selector,
                    val[:40],
                )
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
            variable_name = (
                cred_name
                if is_credential
                else self._suggest_variable_name(selector, val, propagations)
            )
            from src.utils.perf_test_classification import looks_like_person_name

            if looks_like_person_name(val) and variable_name in (
                "input_value",
                "input",
                "field",
            ):
                variable_name = "employee_name"

            parameterizable_candidates.append({
                "selector": selector,
                "value": val,
                "variable_name": variable_name,
                "is_credential": is_credential,
                "credential_name": cred_name,
                "propagations": list(dict.fromkeys(propagations)),  # stable unique
            })

        return parameterizable_candidates

    def _suggest_variable_name(
        self,
        selector: str,
        value: str,
        propagations: Optional[List[str]] = None,
    ) -> str:
        """Derive a stable load-test variable name.

        Prefers network field names from observed propagation (e.g. ``remarks``,
        ``nameOrId``) over fragile CSS paths or raw typed values.

        Args:
            selector: Playwright selector associated with the input.
            value: Literal input value used as a final naming fallback.
            propagations: Optional human-readable propagation strings.

        Returns:
            A sanitized variable name suitable for generated scripts.
        """
        # Prefer the first body/query field seen on the wire.
        for prop in propagations or []:
            for pattern in (
                r"Body field `([^`]+)`",
                r"Query `([^`]+)`",
            ):
                match = re.search(pattern, prop)
                if match:
                    field = match.group(1)
                    # Use last path segment for nested JSON: data.remarks → remarks
                    leaf = field.split(".")[-1].split("/")[-1]
                    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", leaf).strip("_").lower()
                    if cleaned and not cleaned.isdigit() and cleaned not in ("raw", "body"):
                        return cleaned

        if is_correlation_field_selector(selector):
            return suggest_correlation_var_name(selector, "input_value")
        for pattern in [
            r'has-text\(\s*["\']([^"\']+)["\']',
            r'name=["\']?(\w+)',
            r'id=["\']?(\w+)',
            r'placeholder\*?=["\']?(\w+)',
            r'#(\w+)',
            r'data-testid=["\']?([\w-]+)',
        ]:
            match = re.search(pattern, selector, re.IGNORECASE)
            if match:
                label = match.group(1).lower()
                cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", label).strip("_")
                if cleaned and not cleaned.isdigit():
                    return cleaned

        # Avoid childish names from raw typed text ("john_michael_doe", "12300").
        if is_login_field_selector(selector):
            if re.search(r'password|passwd|\[type\s*=\s*["\']?password', selector, re.I):
                return "password"
            return "username"
        return "input_value"

    def _find_propagations(
        self, val: str, val_lower: str, requests: List[Dict[str, Any]]
    ) -> List[str]:
        """Find request locations containing a UI input value.

        Args:
            val: Original input value.
            val_lower: Lowercase input value used for matching.
            requests: Captured request dictionaries to inspect.

        Returns:
            Human-readable query, body, and header propagation descriptions.
        """
        propagations = []
        for req in requests:
            req_url = req.get("url", "")
            req_method = req.get("method", "")

            try:
                parsed = urlparse(req_url)
                for q_key, q_vals in parse_qs(parsed.query).items():
                    if any(self._matches_value(val_lower, qv) for qv in q_vals):
                        propagations.append(
                            f"`{req_method}` Query `{q_key}` in `{req_url}`"
                        )
            except Exception:
                pass

            post_data = req.get("post_data")
            body_type = req.get("body_type") or ""
            if post_data is not None and post_data != "":
                if not isinstance(post_data, (dict, list)):
                    post_data, body_type = parse_post_data(
                        post_data, content_type_from_headers(req.get("headers") or {})
                    )
                fields = flatten_body_fields(post_data)
                matched_field = False
                for field_path, field_val in fields.items():
                    if self._matches_value(val_lower, field_val):
                        propagations.append(
                            f"`{req_method}` Body field `{field_path}` ({body_type or 'body'}) in `{req_url}`"
                        )
                        matched_field = True
                # Fall back to the serialized body for nested or unsupported
                # encodings that could not be represented as flattened fields.
                if not matched_field:
                    try:
                        post_str = (
                            json.dumps(post_data)
                            if isinstance(post_data, (dict, list))
                            else str(post_data)
                        )
                        if self._matches_value(val_lower, post_str):
                            propagations.append(
                                f"`{req_method}` Post Body in `{req_url}`"
                            )
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
        """Test whether a captured field contains an input value.

        Args:
            val_lower: Lowercase source value.
            target_text: Captured field value to inspect.

        Returns:
            ``True`` for a whole-word short match or substring longer match.
        """
        if not target_text:
            return False
        target_lower = str(target_text).lower()
        if len(val_lower) <= 2:
            return bool(re.search(rf"\b{re.escape(val_lower)}\b", target_lower))
        return val_lower in target_lower
