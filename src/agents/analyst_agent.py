import json
import logging
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

class TrafficAnalystAgent:
    def __init__(self):
        pass

    def _extract_query_params(self, url: str) -> Dict[str, List[str]]:
        try:
            parsed = urlparse(url)
            return parse_qs(parsed.query)
        except Exception:
            return {}

    def _get_json_paths(self, data: Any, current_path: str = "$") -> Dict[str, Any]:
        """Recursive helper to build flat dictionary of JSON paths and their values."""
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

    def analyze_runs(self, run1: Dict[str, Any], run2: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        Compares Run 1 and Run 2 network traffic to detect dynamic values and trace their origins.
        """
        reqs1 = run1.get("network_requests", [])
        reqs2 = run2.get("network_requests", [])

        correlations = []
        dependencies = []

        # Step 1: Align requests by matching index, method, and URL path
        # (Assuming the sequential journey is executed identically)
        aligned_pairs = []
        for r1 in reqs1:
            # Look for matching request in run2 by path & method sequence
            path1 = urlparse(r1["url"]).path
            for r2 in reqs2:
                path2 = urlparse(r2["url"]).path
                if r1["method"] == r2["method"] and path1 == path2 and r2 not in [p[1] for p in aligned_pairs]:
                    aligned_pairs.append((r1, r2))
                    break

        logger.info(f"Aligned {len(aligned_pairs)} request pairs for differential analysis.")

        # Step 2: Compare parameters, headers, bodies to find differences (dynamic candidates)
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
                        "run1_value": val1,
                        "run2_value": val2,
                        "reason": "Query parameter changes between executions"
                    })

            # B. Headers
            h1 = r1.get("headers", {})
            h2 = r2.get("headers", {})
            # We focus on common correlation headers or authorization tokens
            interested_headers = ["authorization", "csrf-token", "x-csrf-token", "cookie", "x-request-id"]
            for key in h1.keys():
                normalized_key = key.lower()
                val1 = h1.get(key, "")
                val2 = h2.get(key, "")
                if val1 != val2 and len(val1) > 3:
                    # If it's a cookie header, we will handle individual cookies, but flag authorization/token headers
                    if "cookie" not in normalized_key:
                        dynamic_candidates.append({
                            "request_url": r1["url"],
                            "method": r1["method"],
                            "location": "header",
                            "key": key,
                            "run1_value": val1,
                            "run2_value": val2,
                            "reason": "Header value changes between executions"
                        })

            # C. JSON/Form Post Bodies
            body1 = r1.get("post_data")
            body2 = r2.get("post_data")
            if body1 and body2:
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
                                "json_path": path,
                                "run1_value": val1,
                                "run2_value": val2,
                                "reason": "Post body JSON element changes between executions"
                            })
                elif isinstance(body1, str) and isinstance(body2, str):
                    if body1 != body2 and len(body1) > 3:
                        # Raw body difference
                        dynamic_candidates.append({
                            "request_url": r1["url"],
                            "method": r1["method"],
                            "location": "body",
                            "key": "raw",
                            "run1_value": body1,
                            "run2_value": body2,
                            "reason": "Raw post body differs between executions"
                        })

        # Step 3: Origin / Dependency Tracing (Where did the value first appear?)
        for candidate in dynamic_candidates:
            val1 = candidate["run1_value"]
            val2 = candidate["run2_value"]

            # We search in previous responses of Run 1
            found_origin = False
            for prev_r1, prev_r2 in aligned_pairs:
                # Stop when we reach the current candidate request to ensure chronological origin
                if prev_r1["url"] == candidate["request_url"] and prev_r1["method"] == candidate["method"]:
                    break

                # A. Search Response Headers (e.g. Set-Cookie, Authorization response headers)
                for h_key, h_val in prev_r1.get("response_headers", {}).items():
                    if val1 in h_val:
                        # Also check if it matches in run 2
                        h_val2 = prev_r2.get("response_headers", {}).get(h_key, "")
                        if val2 in h_val2:
                            dependencies.append({
                                "source_request": prev_r1["url"],
                                "source_location": f"header.{h_key}",
                                "target_request": candidate["request_url"],
                                "target_location": f"{candidate['location']}.{candidate.get('key') or candidate.get('json_path')}",
                                "value_key": candidate.get("key") or candidate.get("json_path", "token")
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
                                    "target_request": candidate["request_url"],
                                    "target_location": f"{candidate['location']}.{candidate.get('key') or candidate.get('json_path')}",
                                    "value_key": candidate.get("key") or candidate.get("json_path", "token")
                                })
                                found_origin = True
                                break
                except Exception:
                    # Non-JSON or parsing error
                    if val1 in resp_body1 and val2 in resp_body2:
                        dependencies.append({
                            "source_request": prev_r1["url"],
                            "source_location": "body.raw",
                            "target_request": candidate["request_url"],
                            "target_location": f"{candidate['location']}.{candidate.get('key') or candidate.get('json_path')}",
                            "value_key": candidate.get("key") or candidate.get("json_path", "token")
                        })
                        found_origin = True

                if found_origin:
                    break

            correlations.append(candidate)

        return correlations, dependencies
