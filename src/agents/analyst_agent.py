"""Compares repeated captures to discover dynamic values and dependencies."""

import json
import logging
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

class TrafficAnalystAgent:
    """Detects cross-run value changes and traces response-to-request reuse."""

    def __init__(self) -> None:
        """Create a stateless analyst; no configuration is required."""

    def _extract_query_params(self, url: str) -> Dict[str, List[str]]:
        """Parse query parameters from a captured URL.

        Args:
            url: Absolute or relative request URL.

        Returns:
            Query values grouped by parameter name, or an empty dictionary for
            malformed input.
        """
        try:
            parsed = urlparse(url)
            return parse_qs(parsed.query)
        except Exception:
            return {}

    def _get_json_paths(self, data: Any, current_path: str = "$") -> Dict[str, Any]:
        """Flatten nested JSON-compatible data into scalar JSON paths.

        Args:
            data: Dictionary, list, scalar, or ``None`` to traverse.
            current_path: JSON path prefix for the current recursion level.

        Returns:
            A mapping of leaf JSON paths to stringified non-null values.
        """
        paths = {}
        if isinstance(data, dict):
            for k, v in data.items():
                paths.update(self._get_json_paths(v, f"{current_path}.{k}"))
        elif isinstance(data, list):
            for idx, item in enumerate(data):
                paths.update(self._get_json_paths(item, f"{current_path}[{idx}]"))
        else:
            if data is not None:
                paths[current_path] = str(data)
        return paths

    def _generate_dynamic_name(self, location: str, key: str) -> str:
        """Generate a script-safe name for a dynamic request value.

        Args:
            location: Request area such as ``query``, ``header``, or ``body``.
            key: Field name or JSON path identifying the value.

        Returns:
            A lowercase identifier suitable for correlation variables.
        """
        name = key
        if location == "body" and key == "raw":
            name = "raw_body"
        elif location == "body":
            # extract last key from json path, e.g. $.session.token -> token
            parts = [p for p in re.split(r'[\.\[\]\'"]', key) if p and p != '$' and not p.isdigit()]
            name = parts[-1] if parts else "body_param"
            
        clean_name = re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_').lower()
        if not clean_name or clean_name.isdigit():
            clean_name = f"dynamic_{location}_{clean_name}"
        return clean_name

    def _parse_cookies(self, cookie_header: str) -> Dict[str, str]:
        """Parse a Cookie or Set-Cookie header into name-value pairs.

        Args:
            cookie_header: Semicolon-delimited HTTP cookie header value.

        Returns:
            Cookie values keyed by name, excluding Set-Cookie attributes.
        """
        cookies = {}
        if not cookie_header:
            return cookies
        skip_attrs = {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}
        for part in cookie_header.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                name = name.strip()
                if name.lower() in skip_attrs:
                    continue
                cookies[name] = value.strip()
        return cookies

    def _is_client_side_value(self, val: str) -> bool:
        """Identify timestamps likely generated locally by the browser.

        Args:
            val: Candidate dynamic value.

        Returns:
            ``True`` when the value resembles a Unix or ISO-like timestamp.
        """
        val_str = str(val).strip()
        # UNIX timestamp (10 or 13 digits)
        if val_str.isdigit() and len(val_str) in [10, 13]:
            return True
        # Float timestamp (e.g. 1784131959.123)
        if re.match(r'^\d{10}\.\d+$', val_str):
            return True
        # Standard Date string (e.g. 2026-07-15T21:48:53)
        if re.match(r'^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}', val_str):
            return True
        return False

    def analyze_runs(self, run1: Dict[str, Any], run2: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Compare two captures and trace changing values to earlier responses.

        Args:
            run1: First run record containing captured network requests.
            run2: Second run record containing captured network requests.

        Returns:
            A tuple of dynamic correlation candidates and traced dependencies.
        """
        reqs1 = run1.get("network_requests", [])
        reqs2 = run2.get("network_requests", [])

        correlations = []
        dependencies = []

        # Prefer step-aware alignment so repeated endpoints map to the same UI
        # phase. Fall back to method/path when step metadata is unavailable.
        aligned_pairs = []
        for r1 in reqs1:
            path1 = urlparse(r1["url"]).path
            matched = False
            # First try matching with step_index
            for r2 in reqs2:
                path2 = urlparse(r2["url"]).path
                if (r1["method"] == r2["method"] and 
                    path1 == path2 and 
                    r1.get("step_index") == r2.get("step_index") and 
                    r2 not in [p[1] for p in aligned_pairs]):
                    aligned_pairs.append((r1, r2))
                    matched = True
                    break
            # Fallback to general path matching if step_index is not set or not matching
            if not matched:
                for r2 in reqs2:
                    path2 = urlparse(r2["url"]).path
                    if (r1["method"] == r2["method"] and 
                        path1 == path2 and 
                        r2 not in [p[1] for p in aligned_pairs]):
                        aligned_pairs.append((r1, r2))
                        break

        logger.info(f"Aligned {len(aligned_pairs)} request pairs for differential analysis.")

        # Compare each aligned request surface independently so the eventual
        # dependency records retain the exact injection location.
        dynamic_candidates = [] # list of dicts with details

        for r1, r2 in aligned_pairs:
            url_path = urlparse(r1["url"]).path

            # A. Query Parameters
            q1 = self._extract_query_params(r1["url"])
            q2 = self._extract_query_params(r2["url"])
            all_query_keys = set(q1.keys()).union(q2.keys())
            for key in all_query_keys:
                val1 = q1.get(key, [""])[0]
                val2 = q2.get(key, [""])[0]
                if val1 != val2 and len(val1) > 2:  # filter out extremely short dynamic numbers like 0 vs 1 unless meaningful
                    dynamic_candidates.append({
                        "request_url": r1["url"],
                        "method": r1["method"],
                        "location": "query",
                        "key": key,
                        "dynamic_name": self._generate_dynamic_name("query", key),
                        "run1_value": val1,
                        "run2_value": val2,
                        "reason": "Query parameter changes between executions",
                        "step_index": r1.get("step_index", -1),
                        "step_action": r1.get("step_action", "unknown")
                    })

            # B. Headers (authorization, CSRF, etc.)
            h1 = r1.get("headers", {})
            h2 = r2.get("headers", {})
            for key in h1.keys():
                normalized_key = key.lower()
                val1 = h1.get(key, "")
                val2 = h2.get(key, "")
                if val1 != val2 and len(val1) > 3 and "cookie" not in normalized_key:
                    dynamic_candidates.append({
                        "request_url": r1["url"],
                        "method": r1["method"],
                        "location": "header",
                        "key": key,
                        "dynamic_name": self._generate_dynamic_name("header", key),
                        "run1_value": val1,
                        "run2_value": val2,
                        "reason": "Header value changes between executions",
                        "step_index": r1.get("step_index", -1),
                        "step_action": r1.get("step_action", "unknown")
                    })

            # B2. Individual cookie values from Cookie request header
            cookies1 = self._parse_cookies(h1.get("cookie", h1.get("Cookie", "")))
            cookies2 = self._parse_cookies(h2.get("cookie", h2.get("Cookie", "")))
            for cookie_name in set(cookies1.keys()).union(cookies2.keys()):
                val1 = cookies1.get(cookie_name, "")
                val2 = cookies2.get(cookie_name, "")
                if val1 != val2 and len(val1) > 2:
                    dynamic_candidates.append({
                        "request_url": r1["url"],
                        "method": r1["method"],
                        "location": "cookie",
                        "key": cookie_name,
                        "dynamic_name": self._generate_dynamic_name("cookie", cookie_name),
                        "run1_value": val1,
                        "run2_value": val2,
                        "reason": "Cookie value changes between executions",
                        "step_index": r1.get("step_index", -1),
                        "step_action": r1.get("step_action", "unknown")
                    })

            # C. JSON/Form Post Bodies
            body1 = r1.get("post_data")
            body2 = r2.get("post_data")
            if body1 and body2:
                # Normalize form-urlencoded strings into dicts when possible
                from src.utils.http_body import content_type_from_headers, parse_post_data

                if not isinstance(body1, (dict, list)):
                    body1, _ = parse_post_data(
                        body1, content_type_from_headers(r1.get("headers") or {})
                    )
                if not isinstance(body2, (dict, list)):
                    body2, _ = parse_post_data(
                        body2, content_type_from_headers(r2.get("headers") or {})
                    )

                if isinstance(body1, dict) and isinstance(body2, dict):
                    paths1 = self._get_json_paths(body1)
                    paths2 = self._get_json_paths(body2)
                    for path, val1 in paths1.items():
                        val2 = paths2.get(path)
                        if val2 and val1 != val2 and len(val1) > 2:
                            dynamic_candidates.append({
                                "request_url": r1["url"],
                                "method": r1["method"],
                                "location": "body",
                                "key": path,
                                "json_path": path,
                                "dynamic_name": self._generate_dynamic_name("body", path),
                                "run1_value": val1,
                                "run2_value": val2,
                                "reason": "Post body field changes between executions",
                                "step_index": r1.get("step_index", -1),
                                "step_action": r1.get("step_action", "unknown")
                            })
                elif isinstance(body1, str) and isinstance(body2, str):
                    if body1 != body2 and len(body1) > 3:
                        # Raw body difference
                        dynamic_candidates.append({
                            "request_url": r1["url"],
                            "method": r1["method"],
                            "location": "body",
                            "key": "raw",
                            "dynamic_name": self._generate_dynamic_name("body", "raw"),
                            "run1_value": body1,
                            "run2_value": body2,
                            "reason": "Raw post body differs between executions",
                            "step_index": r1.get("step_index", -1),
                            "step_action": r1.get("step_action", "unknown")
                        })

        # Step 3: Origin / Dependency Tracing (Where did the value first appear?)
        for candidate in dynamic_candidates:
            val1 = candidate["run1_value"]
            val2 = candidate["run2_value"]
            target_loc = f"{candidate['location']}.{candidate.get('key') or candidate.get('json_path')}"

            # Skip client-side generated values like timestamps/dates from response-origin tracing
            if self._is_client_side_value(val1) or self._is_client_side_value(val2):
                correlations.append(candidate)
                continue

            # Search only preceding aligned responses: a value cannot originate
            # from a response that occurs after its target request.
            found_origin = False
            for prev_r1, prev_r2 in aligned_pairs:
                # Stop when we reach the current candidate request to ensure chronological origin
                if prev_r1["url"] == candidate["request_url"] and prev_r1["method"] == candidate["method"]:
                    break

                # A. Search Response Headers (Set-Cookie, Authorization, CSRF tokens, Locations, custom headers)
                for h_key, h_val in prev_r1.get("response_headers", {}).items():
                    h_key_lower = h_key.lower()
                    if h_key_lower not in [
                        "set-cookie", "x-csrf-token", "csrf-token", "authorization",
                        "location", "x-session-id", "token", "x-xsrf-token",
                        "x-auth-token", "x-access-token", "www-authenticate",
                    ]:
                        continue

                    # For Set-Cookie, match individual cookie values
                    if h_key_lower == "set-cookie":
                        set_cookies1 = self._parse_cookies(h_val)
                        set_cookies2 = self._parse_cookies(
                            prev_r2.get("response_headers", {}).get(h_key, "")
                        )
                        for c_name, c_val1 in set_cookies1.items():
                            c_val2 = set_cookies2.get(c_name, "")
                            if candidate["location"] == "cookie" and candidate["key"] == c_name:
                                if val1 == c_val1 and val2 == c_val2:
                                    dependencies.append({
                                        "source_request": prev_r1["url"],
                                        "source_location": f"header.set-cookie.{c_name}",
                                        "source_step_index": prev_r1.get("step_index", -1),
                                        "source_step_action": prev_r1.get("step_action", "unknown"),
                                        "target_request": candidate["request_url"],
                                        "target_location": target_loc,
                                        "target_step_index": candidate.get("step_index", -1),
                                        "target_step_action": candidate.get("step_action", "unknown"),
                                        "value_key": candidate.get("dynamic_name", "token"),
                                        "run1_value": val1,
                                        "run2_value": val2,
                                        "correlation_type": "response_extract",
                                        "confidence": "high",
                                    })
                                    found_origin = True
                                    break
                    elif val1 in h_val:
                        h_val2 = prev_r2.get("response_headers", {}).get(h_key, "")
                        if val2 in h_val2:
                            dependencies.append({
                                "source_request": prev_r1["url"],
                                "source_location": f"header.{h_key}",
                                "source_step_index": prev_r1.get("step_index", -1),
                                "source_step_action": prev_r1.get("step_action", "unknown"),
                                "target_request": candidate["request_url"],
                                "target_location": target_loc,
                                "target_step_index": candidate.get("step_index", -1),
                                "target_step_action": candidate.get("step_action", "unknown"),
                                "value_key": candidate.get("dynamic_name", "token"),
                                "run1_value": val1,
                                "run2_value": val2,
                                "correlation_type": "response_extract",
                            })
                            found_origin = True
                            break

                if found_origin:
                    break

                # B. Search Response Body (JSON paths)
                resp_body1 = prev_r1.get("response_body", "")
                resp_body2 = prev_r2.get("response_body", "")

                try:
                    resp_json1 = json.loads(resp_body1)
                    resp_json2 = json.loads(resp_body2)
                    
                    paths_j1 = self._get_json_paths(resp_json1)
                    paths_j2 = self._get_json_paths(resp_json2)

                    for path, r1_val in paths_j1.items():
                        if val1 == r1_val:
                            # Verify if the corresponding path in Run 2 matches Run 2's candidate value
                            if paths_j2.get(path) == val2:
                                dependencies.append({
                                    "source_request": prev_r1["url"],
                                    "source_location": f"body.{path}",
                                    "source_step_index": prev_r1.get("step_index", -1),
                                    "source_step_action": prev_r1.get("step_action", "unknown"),
                                    "target_request": candidate["request_url"],
                                    "target_location": target_loc,
                                    "target_step_index": candidate.get("step_index", -1),
                                    "target_step_action": candidate.get("step_action", "unknown"),
                                    "value_key": candidate.get("dynamic_name", "token"),
                                    "run1_value": val1,
                                    "run2_value": val2,
                                    "correlation_type": "response_extract",
                                })
                                found_origin = True
                                break
                except Exception:
                    # HTML/text responses cannot provide JSON paths, so retain
                    # a lower-confidence raw-body containment trace.
                    if val1 in resp_body1 and val2 in resp_body2:
                        dependencies.append({
                            "source_request": prev_r1["url"],
                            "source_location": "body.raw",
                            "source_step_index": prev_r1.get("step_index", -1),
                            "source_step_action": prev_r1.get("step_action", "unknown"),
                            "target_request": candidate["request_url"],
                            "target_location": target_loc,
                            "target_step_index": candidate.get("step_index", -1),
                            "target_step_action": candidate.get("step_action", "unknown"),
                            "value_key": candidate.get("dynamic_name", "token"),
                            "run1_value": val1,
                            "run2_value": val2,
                            "correlation_type": "response_extract",
                        })
                        found_origin = True

                if found_origin:
                    break

            correlations.append(candidate)

        # Values reused across request inputs may have no captured response
        # origin. Treat the earliest occurrence as a low-confidence source and
        # later occurrences as passes, while avoiding already-traced targets.
        value_groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for idx, candidate in enumerate(dynamic_candidates):
            group_key = (
                candidate.get("dynamic_name"),
                candidate.get("run1_value"),
                candidate.get("run2_value"),
            )
            value_groups.setdefault(group_key, []).append({**candidate, "_order": idx})

        traced_targets = {
            (d.get("target_request"), d.get("target_location"), d.get("value_key"))
            for d in dependencies
        }

        for (_name, val1, val2), group in value_groups.items():
            if len(group) < 2:
                continue
            group_sorted = sorted(
                group,
                key=lambda c: (c.get("step_index", 10**9), c.get("_order", 0)),
            )
            source = group_sorted[0]
            source_loc = f"{source['location']}.{source.get('key') or source.get('json_path')}"
            for target in group_sorted[1:]:
                target_loc = f"{target['location']}.{target.get('key') or target.get('json_path')}"
                dep_key = (target["request_url"], target_loc, target.get("dynamic_name"))
                if dep_key in traced_targets:
                    continue
                if (
                    source["request_url"] == target["request_url"]
                    and source_loc == target_loc
                ):
                    continue
                dependencies.append({
                    "source_request": source["request_url"],
                    "source_location": source_loc,
                    "source_step_index": source.get("step_index", -1),
                    "source_step_action": source.get("step_action", "unknown"),
                    "target_request": target["request_url"],
                    "target_location": target_loc,
                    "target_step_index": target.get("step_index", -1),
                    "target_step_action": target.get("step_action", "unknown"),
                    "value_key": source.get("dynamic_name", "token"),
                    "run1_value": val1,
                    "run2_value": val2,
                    "correlation_type": "shared_value",
                    "confidence": "low",
                })
                traced_targets.add(dep_key)

        return correlations, dependencies
