"""Compares repeated captures to discover dynamic values and dependencies."""

import json
import logging
import re
from typing import List, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs

from src.utils.correlation_noise import (
    is_actionable_correlation,
    is_actionable_dependency,
    is_client_side_query_key,
    is_correlatable_request_header,
    is_parameterish_query_key,
)

logger = logging.getLogger(__name__)


class TrafficAnalystAgent:
    """Detects cross-run value changes and traces response-to-request reuse."""

    def __init__(self) -> None:
        """Create a stateless analyst; no configuration is required."""

    @staticmethod
    def _is_correlatable_request_header(header_name: str) -> bool:
        """Return whether a request header is worth treating as correlation."""
        return is_correlatable_request_header(header_name)

    @staticmethod
    def _path_signature(path: str) -> str:
        """Normalize a URL path so numeric ID segments align across runs.

        ``/claim/employees/88/requests`` and ``/claim/employees/99/requests``
        share the same signature so differential analysis can detect the ID.
        """
        segs = []
        for s in (path or "").split("/"):
            if not s:
                continue
            segs.append("{id}" if s.isdigit() else s)
        return "/" + "/".join(segs)

    def _trace_stable_response_ids(
        self,
        aligned_pairs: List[Tuple[Dict[str, Any], Dict[str, Any]]],
        existing: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Correlate JSON IDs into later path/query even when Run1==Run2.

        Differential analysis misses IDs that stay constant across dual runs
        (same employee). Create/list responses still expose them for scripting.
        """
        id_leaf_re = re.compile(
            r"(referenceid|empnumber|employeeid|claimrequestid|requestid|^id$)",
            re.IGNORECASE,
        )
        noise_path_re = re.compile(
            r"(subunit|location|dashboard|buzz|shortcut|action-sum|leave)",
            re.IGNORECASE,
        )
        seen = {
            (
                d.get("value_key"),
                d.get("source_request"),
                d.get("target_request"),
                d.get("target_location"),
            )
            for d in existing
        }
        out: List[Dict[str, Any]] = []
        for i, (src1, _src2) in enumerate(aligned_pairs):
            src_url = str(src1.get("url") or "")
            src_l = src_url.lower()
            # Dashboard / widget payloads cause coincidental id matches (e.g. subunit.id=10)
            if any(
                h in src_l
                for h in (
                    "/dashboard/",
                    "/buzz/",
                    "/events/push",
                    "/i18n/",
                    "/leave/",
                )
            ):
                continue
            try:
                j1 = json.loads(src1.get("response_body") or "")
                j2 = json.loads((_src2.get("response_body") or ""))
            except Exception:
                continue
            paths1 = self._get_json_paths(j1)
            paths2 = self._get_json_paths(j2)
            for path, val1 in paths1.items():
                leaf = path.split(".")[-1].split("[")[0]
                if not id_leaf_re.search(leaf):
                    continue
                if noise_path_re.search(path):
                    continue
                # Generic `id` only from claim create payloads ($.data.id)
                if leaf.lower() == "id":
                    if "/api/v2/claim" not in src_l and "/claim/" not in src_l:
                        continue
                    if path != "$.data.id":
                        continue
                val1_s = str(val1)
                val2_s = str(paths2.get(path, ""))
                if not val1_s:
                    continue
                if not (val1_s.isdigit() or len(val1_s) >= 8):
                    continue
                if val1_s.isdigit() and len(val1_s) < 2:
                    continue
                var = self._generate_dynamic_name("body", path)
                # Prefer business names for claim request id
                if leaf.lower() == "id" and "request" in src_l:
                    var = "requestId"
                for later1, _later2 in aligned_pairs[i + 1 :]:
                    url1 = later1.get("url") or ""
                    segs = [s for s in urlparse(url1).path.split("/") if s]
                    q1 = self._extract_query_params(url1)
                    if val1_s in segs:
                        try:
                            idx = segs.index(val1_s)
                            prev = segs[idx - 1] if idx > 0 else "id"
                        except ValueError:
                            prev = "id"
                        prev_l = prev.lower()
                        if prev_l not in {
                            "employees",
                            "employee",
                            "requests",
                            "request",
                            "claims",
                            "claim",
                            "id",
                        } and not prev_l.endswith("id"):
                            continue
                        # Generic id → path.requests requires claim create source
                        if prev_l in ("requests", "request", "id") and leaf.lower() == "id":
                            if "/claim/" not in src_l:
                                continue
                        tgt_loc = f"path.{prev}"
                        key = (var, src1.get("url"), url1, tgt_loc)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({
                            "source_request": src1.get("url"),
                            "source_location": f"body.{path}",
                            "source_step_index": src1.get("step_index", -1),
                            "source_step_action": src1.get("step_action", "unknown"),
                            "target_request": url1,
                            "target_location": tgt_loc,
                            "target_step_index": later1.get("step_index", -1),
                            "target_step_action": later1.get("step_action", "unknown"),
                            "value_key": var,
                            "run1_value": val1_s,
                            "run2_value": val2_s or val1_s,
                            "correlation_type": "response_extract",
                            "confidence": "high",
                        })
                    for qk, qvals in q1.items():
                        if not qvals or str(qvals[0]) != val1_s:
                            continue
                        if is_client_side_query_key(qk) or is_parameterish_query_key(qk):
                            continue
                        tgt_loc = f"query.{qk}"
                        key = (var, src1.get("url"), url1, tgt_loc)
                        if key in seen:
                            continue
                        seen.add(key)
                        out.append({
                            "source_request": src1.get("url"),
                            "source_location": f"body.{path}",
                            "source_step_index": src1.get("step_index", -1),
                            "source_step_action": src1.get("step_action", "unknown"),
                            "target_request": url1,
                            "target_location": tgt_loc,
                            "target_step_index": later1.get("step_index", -1),
                            "target_step_action": later1.get("step_action", "unknown"),
                            "value_key": var,
                            "run1_value": val1_s,
                            "run2_value": val2_s or val1_s,
                            "correlation_type": "response_extract",
                            "confidence": "high",
                        })
        return self._prefer_best_dependencies(out)

    @staticmethod
    def _prefer_best_dependencies(deps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep one edge per (target_loc, value); prefer claim-create JSON paths."""
        best: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

        def score(d: Dict[str, Any]) -> Tuple[int, int, int]:
            src = str(d.get("source_location") or "")
            url = str(d.get("source_request") or "").lower()
            var = str(d.get("value_key") or "").lower()
            s = 0
            if "subunit" in src or "dashboard" in url:
                s -= 50
            if "/claim/" in url and "employees" in url and "requests" in url:
                s += 20
            if src.endswith("$.data.id") or src == "body.$.data.id":
                s += 30
            if "referenceid" in src.lower() or "empnumber" in src.lower():
                s += 25
            if var in ("referenceid", "empnumber", "requestid"):
                s += 10
            if "[" in src:
                s -= 5
            return (s, -len(src), len(str(d.get("run1_value") or "")))

        for d in deps:
            # Collapse aliases (requests vs requestId) that pass the same literal
            key = (
                str(d.get("target_location") or ""),
                str(d.get("run1_value") or ""),
            )
            prev = best.get(key)
            if prev is None or score(d) > score(prev):
                best[key] = d
        return list(best.values())

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
        # phase. Fall back to method/path signature (numeric IDs wildcarded).
        aligned_pairs = []
        for r1 in reqs1:
            path1 = urlparse(r1["url"]).path
            sig1 = self._path_signature(path1)
            matched = False
            # First try matching with step_index
            for r2 in reqs2:
                path2 = urlparse(r2["url"]).path
                sig2 = self._path_signature(path2)
                if (r1["method"] == r2["method"] and
                    (path1 == path2 or sig1 == sig2) and
                    r1.get("step_index") == r2.get("step_index") and
                    r2 not in [p[1] for p in aligned_pairs]):
                    aligned_pairs.append((r1, r2))
                    matched = True
                    break
            # Fallback to general path matching if step_index is not set or not matching
            if not matched:
                for r2 in reqs2:
                    path2 = urlparse(r2["url"]).path
                    sig2 = self._path_signature(path2)
                    if (r1["method"] == r2["method"] and
                        (path1 == path2 or sig1 == sig2) and
                        r2 not in [p[1] for p in aligned_pairs]):
                        aligned_pairs.append((r1, r2))
                        break

        logger.info(f"Aligned {len(aligned_pairs)} request pairs for differential analysis.")

        # Compare each aligned request surface independently so the eventual
        # dependency records retain the exact injection location.
        dynamic_candidates = [] # list of dicts with details

        for r1, r2 in aligned_pairs:
            url_path = urlparse(r1["url"]).path

            # A0. Numeric path segments that change across runs (e.g. /employees/88)
            try:
                segs1 = [s for s in urlparse(r1["url"]).path.split("/") if s]
                segs2 = [s for s in urlparse(r2["url"]).path.split("/") if s]
                if len(segs1) == len(segs2):
                    for i, (a, b) in enumerate(zip(segs1, segs2)):
                        if a == b:
                            continue
                        if not (a.isdigit() and b.isdigit()):
                            continue
                        prev = segs1[i - 1] if i > 0 else "id"
                        # Only correlate under API resource parents (not soft page paths)
                        entity_segs = {
                            "employees",
                            "employee",
                            "requests",
                            "request",
                            "claims",
                            "claim",
                            "users",
                            "user",
                            "orders",
                            "order",
                            "id",
                        }
                        prev_l = prev.lower()
                        if prev_l not in entity_segs and not prev_l.endswith("id"):
                            continue
                        # Skip soft PIM page paths like /viewEmployee/empNumber/22
                        url_l = (r1.get("url") or "").lower()
                        if "/viewemployee/" in url_l or "/pim/view" in url_l:
                            continue
                        if len(a) < 2 and len(b) < 2:
                            continue
                        dynamic_candidates.append({
                            "request_url": r1["url"],
                            "method": r1["method"],
                            "location": "path",
                            "key": prev,
                            "dynamic_name": self._generate_dynamic_name("path", prev),
                            "run1_value": a,
                            "run2_value": b,
                            "reason": "Path ID segment changes between executions",
                            "step_index": r1.get("step_index", -1),
                            "step_action": r1.get("step_action", "unknown"),
                        })
            except Exception:
                pass

            # A. Query Parameters
            q1 = self._extract_query_params(r1["url"])
            q2 = self._extract_query_params(r2["url"])
            all_query_keys = set(q1.keys()).union(q2.keys())
            for key in all_query_keys:
                # Cache-busters and search filters are not extract→pass correlations.
                if is_client_side_query_key(key) or is_parameterish_query_key(key):
                    continue
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

            # B. Auth / CSRF request headers only (not generic browser headers).
            # Cookie state is handled separately via cookie location + cookie jar.
            h1 = r1.get("headers", {})
            h2 = r2.get("headers", {})
            for key in h1.keys():
                if not is_correlatable_request_header(key):
                    continue
                val1 = h1.get(key, "")
                val2 = h2.get(key, "")
                if val1 != val2 and len(str(val1)) > 3:
                    dynamic_candidates.append({
                        "request_url": r1["url"],
                        "method": r1["method"],
                        "location": "header",
                        "key": key,
                        "dynamic_name": self._generate_dynamic_name("header", key),
                        "run1_value": val1,
                        "run2_value": val2,
                        "reason": (
                            "Auth/CSRF header changes between executions "
                            "(generic request headers are ignored)"
                        ),
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
                    # Require long tokens — short digits match almost any payload.
                    if (
                        len(str(val1)) >= 8
                        and val1 in resp_body1
                        and val2 in resp_body2
                    ):
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

        # Stable server IDs (same across runs) still need extract→pass when a
        # create/list JSON response value appears as a later path/query segment.
        dependencies.extend(
            self._trace_stable_response_ids(aligned_pairs, dependencies)
        )
        dependencies = self._prefer_best_dependencies(dependencies)

        # Drop browser-noise candidates and dependencies before returning.
        correlations = [c for c in correlations if is_actionable_correlation(c)]
        dependencies = [d for d in dependencies if is_actionable_dependency(d)]
        dependencies = self._prefer_best_dependencies(dependencies)
        return correlations, dependencies
