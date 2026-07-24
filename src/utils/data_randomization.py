"""HTTP payload-level data randomization for capture / replay isolation.

Operates strictly on raw HTTP requests (JSON bodies, form-urlencoded bodies,
query strings, and selected headers). No Playwright UI locators or ``page.fill``
semantics are used.

Pipeline role
-------------
Run 1 (Watch-me / first capture)
    Inspect outgoing traffic and harvest hardcoded state-mutating payload
    values into a transformation plan.

Run 2 (headless replay)
    Intercept requests via ``page.route`` before they hit the network, rewrite
    matching payload leaves with randomized values, and optionally mock
    non-randomizable third-party endpoints.

Correlation engine
    Consumes the value-mapping ledger so deliberate test-data randomization is
    not mistaken for server-generated tokens (CSRF, session IDs, …).

Load-Test IR / k6
    Non-randomizable routes are flagged so the deterministic emitter can emit
    mocks / manual-data markers instead of live third-party calls.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.utils.http_body import content_type_from_headers, flatten_body_fields, parse_post_data

logger = logging.getLogger(__name__)

# Field-name tokens that typically carry unique business / identity data.
RANDOMIZABLE_FIELD_RE = re.compile(
    r"(?:^|[._\[\]-])("
    r"email|e[-_]?mail|username|user[-_]?name|login[-_]?name|"
    r"order[-_]?id|order[-_]?number|orderid|ordernumber|"
    r"invoice[-_]?id|invoice[-_]?number|"
    r"claim[-_]?id|claim[-_]?name|reference|ref[-_]?id|ref[-_]?no|"
    r"phone|mobile|cellphone|tel|"
    r"sku|external[-_]?id|idempotency[-_]?key|"
    r"employee[-_]?id|emp[-_]?number|empnumber|"
    r"account[-_]?number|account[-_]?id|"
    r"nickname|display[-_]?name"
    r")(?:$|[._\[\]-])",
    re.IGNORECASE,
)

# Never rewrite these — they break auth or are server-owned.
PROTECTED_FIELD_RE = re.compile(
    r"(?:^|[._\[\]-])("
    r"password|passwd|pwd|pass|"
    r"csrf|xsrf|token|_token|authenticity[-_]?token|"
    r"authorization|session|cookie|nonce|"
    r"captcha|recaptcha|otp|pin|"
    r"signature|hmac|digest"
    r")(?:$|[._\[\]-])",
    re.IGNORECASE,
)

AUTH_PATH_HINTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth",
    "/oauth",
    "/session",
    "/authenticate",
    "/sso",
    "/token",
)

NON_RANDOMIZABLE_HOST_HINTS = (
    "stripe.com",
    "stripe.network",
    "paypal.com",
    "paypalobjects.com",
    "braintreegateway.com",
    "braintree-api.com",
    "adyen.com",
    "checkout.stripe.com",
    "js.stripe.com",
    "api.stripe.com",
    "payments.google.com",
    "square.com",
    "squareup.com",
    "worldpay.com",
    "authorize.net",
    "klarna.com",
    "afterpay.com",
)

NON_RANDOMIZABLE_PATH_HINTS = (
    "/payment",
    "/payments",
    "/checkout/pay",
    "/billing/charge",
    "/card/tokenize",
    "/3ds",
    "/three-d-secure",
)

MOCK_JSON_BODY = json.dumps(
    {
        "ok": True,
        "mocked": True,
        "nfe": "non_randomizable_endpoint",
        "message": "Mocked during Run 2 replay to avoid third-party side effects",
    }
)

# Strategy names shared with k6_generator._randomized_var_js
STRATEGY_EMAIL = "email_timestamp"
STRATEGY_UUID = "uuid"
STRATEGY_NUMERIC = "numeric_timestamp"
STRATEGY_PHONE = "phone_timestamp"
STRATEGY_SUFFIX = "suffix_timestamp"


@dataclass
class PayloadTransform:
    """One harvested HTTP payload leaf rewritten on Run 2."""

    field_path: str
    run1_value: str
    location: str  # body | query | header
    method: str
    url_path: str
    strategy: str = STRATEGY_SUFFIX
    run2_value: Optional[str] = None
    request_url: str = ""


@dataclass
class NonRandomizableRoute:
    """An HTTP route that cannot safely be mutated at the payload level."""

    method: str
    url: str
    url_path: str
    reason: str
    handling: str = "mock_response"  # mock_response | flag_ir


def is_protected_field(field_path: str) -> bool:
    """Return True when a payload field must never be rewritten."""
    return bool(PROTECTED_FIELD_RE.search(field_path or ""))


def is_randomizable_field(field_path: str) -> bool:
    """Return True when a payload field looks like unique test data."""
    path = field_path or ""
    if not path or is_protected_field(path):
        return False
    return bool(RANDOMIZABLE_FIELD_RE.search(path))


def is_auth_endpoint(url: str) -> bool:
    """Return True for login / OAuth style paths that must keep credentials."""
    try:
        path = (urlparse(url or "").path or "").lower()
    except Exception:
        path = (url or "").lower()
    return any(hint in path for hint in AUTH_PATH_HINTS)


def classify_non_randomizable_url(url: str, method: str = "") -> Optional[str]:
    """Return a reason string when a URL must not be live-mutated, else None."""
    raw = url or ""
    lower = raw.lower()
    try:
        parsed = urlparse(raw)
        host = (parsed.netloc or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        host, path = "", lower

    for hint in NON_RANDOMIZABLE_HOST_HINTS:
        if hint in host or hint in lower:
            return f"third_party_payment_host:{hint}"

    method_u = (method or "").upper()
    if method_u in ("POST", "PUT", "PATCH", "DELETE"):
        for hint in NON_RANDOMIZABLE_PATH_HINTS:
            if hint in path:
                return f"non_idempotent_payment_path:{hint}"
    return None


def choose_strategy(field_path: str, value: str) -> str:
    """Pick a randomization strategy from field name and sample value shape."""
    lower = (field_path or "").lower()
    if "email" in lower or "@" in (value or ""):
        return STRATEGY_EMAIL
    if re.fullmatch(r"\d+", value or ""):
        return STRATEGY_NUMERIC
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        value or "",
        re.IGNORECASE,
    ):
        return STRATEGY_UUID
    if "phone" in lower or "mobile" in lower or "tel" in lower:
        return STRATEGY_PHONE
    return STRATEGY_SUFFIX


def generate_random_value(
    original: str,
    strategy: str,
    *,
    salt: Optional[str] = None,
) -> str:
    """Produce a Run 2 replacement for a harvested Run 1 literal.

    Strategy names match ``k6_generator._randomized_var_js``.
    """
    original = "" if original is None else str(original)
    stamp = str(int(time.time() * 1000) % 10_000_000_000)
    tag = (salt or uuid.uuid4().hex[:6]).lower()

    if strategy == STRATEGY_UUID:
        return str(uuid.uuid4())

    if strategy == STRATEGY_EMAIL:
        if "@" in original:
            local, _, domain = original.partition("@")
            base = re.sub(r"_nfe_\d+_[a-f0-9]+$", "", local, flags=re.I)
            base = re.sub(r"[+._-]?\d+$", "", base) or "user"
            return f"{base}_nfe_{stamp}_{tag}@{domain}"
        return f"user_nfe_{stamp}_{tag}@example.test"

    if strategy == STRATEGY_NUMERIC:
        digits = re.sub(r"\D", "", original) or "1000"
        return f"{digits}{stamp[-4:]}{int(tag[:4], 16) % 100:02d}"[-18:]

    if strategy == STRATEGY_PHONE:
        digits = re.sub(r"\D", "", original) or "5550000"
        prefix = digits[: max(3, len(digits) - 4)]
        return f"{prefix}{stamp[-4:]}"

    stem = re.sub(r"_nfe_\d+_[a-f0-9]+$", "", original, flags=re.I)
    stem = re.sub(r"[+._-]?\d{4,}$", "", stem).rstrip("._-+") or original or "nfe"
    if len(stem) > 40:
        stem = stem[:40]
    return f"{stem}_nfe_{stamp}_{tag}"


def _set_by_path(body: Any, path: str, new_value: str) -> Any:
    """Set a nested dict/list leaf addressed by a flatten_body_fields path."""
    if not path:
        return body

    tokens: List[Any] = []
    for part in re.split(r"\.", path):
        m = re.match(r"^([^\[\]]+)(?:\[(\d+)\])?$", part)
        if not m:
            tokens.append(part)
            continue
        tokens.append(m.group(1))
        if m.group(2) is not None:
            tokens.append(int(m.group(2)))

    def _walk(node: Any, idx: int) -> Any:
        if idx >= len(tokens):
            return new_value
        key = tokens[idx]
        if isinstance(key, int):
            if not isinstance(node, list):
                return node
            while len(node) <= key:
                node.append(None)
            node[key] = _walk(node[key], idx + 1)
            return node
        if not isinstance(node, dict):
            return node
        node[key] = _walk(node.get(key), idx + 1)
        return node

    if isinstance(body, (dict, list)):
        clone = json.loads(json.dumps(body))
        return _walk(clone, 0)
    return new_value


def _serialize_body(body: Any, body_type: str) -> Optional[str]:
    """Encode a mutated body back to an HTTP wire representation."""
    if body is None:
        return None
    if body_type == "json":
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)
    if body_type == "form":
        if isinstance(body, dict):
            pairs = []
            for k, v in body.items():
                if isinstance(v, list):
                    for item in v:
                        pairs.append((k, "" if item is None else str(item)))
                else:
                    pairs.append((k, "" if v is None else str(v)))
            return urlencode(pairs)
        return str(body)
    return str(body)


def _path_only(url: str) -> str:
    try:
        return urlparse(url or "").path or "/"
    except Exception:
        return "/"


class DataRandomizationMiddleware:
    """Harvest Run 1 HTTP payloads and rewrite / mock traffic during Run 2.

    Public surface matches graph + Playwright integration:

    - ``harvest_from_requests`` — inspect Run 1 captures, return field count
    - ``attach_route`` — install ``page.route`` handler for Run 2
    - ``ledger_entries`` / ``non_randomizable_routes`` — correlation + IR
    - ``to_dict`` / ``from_dict`` — graph state persistence
    """

    def __init__(self) -> None:
        self.transforms: List[PayloadTransform] = []
        self._non_randomizable: List[NonRandomizableRoute] = []
        self._ledger: List[Dict[str, Any]] = []
        self._value_map: Dict[str, str] = {}
        self._route_attached = False

    # ------------------------------------------------------------------ harvest
    def harvest_from_requests(
        self,
        network_requests: Sequence[Dict[str, Any]],
    ) -> int:
        """Inspect Run 1 captures and build the mutation plan.

        Args:
            network_requests: CDP / Playwright request logs from Run 1.

        Returns:
            Number of randomizable payload fields harvested.
        """
        seen_paths: Set[Tuple[str, str, str, str]] = set()
        seen_routes: Set[Tuple[str, str]] = set()

        for req in network_requests or []:
            method = (req.get("method") or "GET").upper()
            url = req.get("url") or ""
            if not url.startswith("http"):
                continue

            url_path = _path_only(url)

            reason = classify_non_randomizable_url(url, method)
            if reason:
                key = (method, url_path)
                if key not in seen_routes:
                    seen_routes.add(key)
                    self._non_randomizable.append(
                        NonRandomizableRoute(
                            method=method,
                            url=url,
                            url_path=url_path,
                            reason=reason,
                            handling="mock_response",
                        )
                    )
                continue

            if is_auth_endpoint(url):
                continue

            for qk, qv in parse_qsl(urlparse(url).query or "", keep_blank_values=True):
                field_path = f"query.{qk}"
                if not is_randomizable_field(qk) and not is_randomizable_field(field_path):
                    continue
                if not qv or is_protected_field(qk):
                    continue
                dedupe = (method, url_path, "query", qk)
                if dedupe in seen_paths:
                    continue
                seen_paths.add(dedupe)
                self._register_transform(
                    PayloadTransform(
                        field_path=field_path,
                        run1_value=str(qv),
                        location="query",
                        method=method,
                        url_path=url_path,
                        strategy=choose_strategy(field_path, str(qv)),
                        request_url=url,
                    )
                )

            raw_body = req.get("post_data")
            if raw_body in (None, "", {}, []):
                continue
            headers = req.get("headers") or {}
            body_type = req.get("body_type") or ""
            parsed_body = raw_body
            if not isinstance(raw_body, (dict, list)):
                parsed_body, body_type = parse_post_data(
                    raw_body, content_type_from_headers(headers)
                )
            if not isinstance(parsed_body, (dict, list)):
                continue

            for path, value in flatten_body_fields(parsed_body).items():
                if not is_randomizable_field(path):
                    continue
                if not value or is_protected_field(path):
                    continue
                if len(str(value)) < 2:
                    continue
                dedupe = (method, url_path, "body", path)
                if dedupe in seen_paths:
                    continue
                seen_paths.add(dedupe)
                self._register_transform(
                    PayloadTransform(
                        field_path=f"payload.{path}",
                        run1_value=str(value),
                        location="body",
                        method=method,
                        url_path=url_path,
                        strategy=choose_strategy(path, str(value)),
                        request_url=url,
                    )
                )

        self._materialize_replacements()
        logger.info(
            "Data randomization harvest: %s transform(s), %s non-randomizable route(s).",
            len(self.transforms),
            len(self._non_randomizable),
        )
        return len(self.transforms)

    def _register_transform(self, transform: PayloadTransform) -> None:
        self.transforms.append(transform)

    def _materialize_replacements(self) -> None:
        """Allocate stable Run 2 values and populate the correlation ledger."""
        for t in self.transforms:
            if t.run2_value:
                self._value_map[t.run1_value] = t.run2_value
                continue
            if t.run1_value in self._value_map:
                t.run2_value = self._value_map[t.run1_value]
            else:
                replacement = generate_random_value(t.run1_value, t.strategy)
                self._value_map[t.run1_value] = replacement
                t.run2_value = replacement
            self._upsert_ledger_entry(t)

    def _upsert_ledger_entry(self, t: PayloadTransform) -> None:
        if not t.run1_value or not t.run2_value:
            return
        for e in self._ledger:
            if (
                e.get("field_path") == t.field_path
                and e.get("run1_value") == t.run1_value
                and e.get("method") == t.method
                and e.get("url_path") == t.url_path
            ):
                e["run2_value"] = t.run2_value
                e["strategy"] = t.strategy
                return
        self._ledger.append(
            {
                "run1_value": t.run1_value,
                "run2_value": t.run2_value,
                "field_path": t.field_path,
                "location": t.location,
                "surface": t.location,
                "method": t.method,
                "url_path": t.url_path,
                "request_url": t.request_url,
                "strategy": t.strategy,
                "kind": "test_data_randomization",
                "source": "http_payload_middleware",
            }
        )

    # -------------------------------------------------------------- introspection
    def ledger_entries(self) -> List[Dict[str, Any]]:
        """Return the protocol value-mapping ledger for the Correlation Engine."""
        return list(self._ledger)

    def non_randomizable_routes(self) -> List[Dict[str, Any]]:
        """Return routes that need mocking or manual IR data provisioning."""
        return [
            {
                "method": r.method,
                "url": r.url,
                "url_path": r.url_path,
                "reason": r.reason,
                "handling": r.handling,
                "requires_manual_data": True,
                "mock_in_load_test": r.handling == "mock_response",
            }
            for r in self._non_randomizable
        ]

    # ------------------------------------------------------------ route rewrite
    def transforms_for_request(self, method: str, url: str) -> List[PayloadTransform]:
        method_u = (method or "GET").upper()
        path = _path_only(url)
        return [
            t
            for t in self.transforms
            if t.method == method_u and t.url_path == path and t.run2_value
        ]

    def rewrite_request(
        self,
        *,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        post_data: Any = None,
    ) -> Dict[str, Any]:
        """Apply planned mutations to one outgoing HTTP request."""
        headers = dict(headers or {})
        method_u = (method or "GET").upper()
        applicable = self.transforms_for_request(method_u, url)
        if not applicable:
            return {
                "url": url,
                "headers": headers,
                "post_data": post_data,
                "modified": False,
            }

        parsed = urlparse(url)
        query_pairs = parse_qsl(parsed.query or "", keep_blank_values=True)
        body = post_data
        body_type = "empty"
        if body not in (None, ""):
            if isinstance(body, (dict, list)):
                body_type = "json"
            else:
                body, body_type = parse_post_data(
                    body, content_type_from_headers(headers)
                )

        modified = False
        for t in applicable:
            if not t.run2_value:
                continue
            if t.location == "query":
                key = t.field_path.split(".", 1)[-1]
                new_pairs = []
                for qk, qv in query_pairs:
                    if qk == key and str(qv) == t.run1_value:
                        new_pairs.append((qk, t.run2_value))
                        modified = True
                    else:
                        new_pairs.append((qk, qv))
                query_pairs = new_pairs
            elif t.location == "body" and isinstance(body, (dict, list)):
                leaf = t.field_path
                if leaf.startswith("payload."):
                    leaf = leaf[len("payload.") :]
                flat = flatten_body_fields(body)
                if flat.get(leaf) == t.run1_value or flat.get(leaf) == str(t.run1_value):
                    body = _set_by_path(body, leaf, t.run2_value)
                    modified = True

        new_url = urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                urlencode(query_pairs),
                parsed.fragment,
            )
        )
        new_post: Any = post_data
        if modified and body_type in ("json", "form") and isinstance(body, (dict, list)):
            new_post = _serialize_body(body, body_type)
        elif modified and isinstance(body, (dict, list)):
            new_post = _serialize_body(body, "json")

        return {
            "url": new_url,
            "headers": headers,
            "post_data": new_post,
            "modified": modified,
        }

    def should_mock(self, method: str, url: str) -> Optional[NonRandomizableRoute]:
        """Return the matching non-randomizable route when mocking applies."""
        method_u = (method or "GET").upper()
        path = _path_only(url)
        for route in self._non_randomizable:
            if route.method == method_u and route.url_path == path:
                if route.handling == "mock_response":
                    return route
        reason = classify_non_randomizable_url(url, method_u)
        if reason:
            return NonRandomizableRoute(
                method=method_u,
                url=url,
                url_path=path,
                reason=reason,
                handling="mock_response",
            )
        return None

    def make_route_handler(self) -> Callable[[Any], None]:
        """Build a Playwright ``page.route`` handler bound to this middleware."""

        def _handler(route: Any) -> None:
            request = route.request
            method = request.method
            url = request.url

            mock = self.should_mock(method, url)
            if mock is not None:
                logger.info(
                    "Mocking non-randomizable %s %s (%s)",
                    method,
                    url,
                    mock.reason,
                )
                try:
                    route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=MOCK_JSON_BODY,
                        headers={
                            "x-nfe-mocked": "1",
                            "x-nfe-mock-reason": (mock.reason or "")[:120],
                        },
                    )
                except Exception as fulfill_err:
                    logger.warning("Mock fulfill failed (%s); continuing.", fulfill_err)
                    route.continue_()
                return

            try:
                post_data = None
                try:
                    post_data = request.post_data
                except Exception:
                    post_data = None
                headers = dict(request.headers or {})
                rewritten = self.rewrite_request(
                    method=method,
                    url=url,
                    headers=headers,
                    post_data=post_data,
                )
                if not rewritten.get("modified"):
                    route.continue_()
                    return

                cont_kwargs: Dict[str, Any] = {"url": rewritten["url"]}
                if rewritten.get("post_data") is not None:
                    cont_kwargs["post_data"] = rewritten["post_data"]
                hdrs = {
                    k: v
                    for k, v in (rewritten.get("headers") or {}).items()
                    if str(k).lower() != "content-length"
                }
                if hdrs:
                    cont_kwargs["headers"] = hdrs
                logger.debug(
                    "Randomized payload for %s %s",
                    method,
                    _path_only(url),
                )
                route.continue_(**cont_kwargs)
            except Exception as err:
                logger.warning("Randomization route handler error (%s); continuing.", err)
                try:
                    route.continue_()
                except Exception:
                    pass

        return _handler

    def attach_route(self, page: Any) -> None:
        """Install ``page.route`` interception for Run 2 payload mutation."""
        if page is None:
            return
        page.route("**/*", self.make_route_handler())
        self._route_attached = True
        logger.info(
            "Data-randomization route interception enabled (%s transform(s)).",
            len(self.transforms),
        )

    # -------------------------------------------------------------- persistence
    def to_dict(self) -> Dict[str, Any]:
        """Serialize middleware plan + ledger for graph state persistence."""
        return {
            "transforms": [
                {
                    "field_path": t.field_path,
                    "run1_value": t.run1_value,
                    "run2_value": t.run2_value,
                    "location": t.location,
                    "method": t.method,
                    "url_path": t.url_path,
                    "strategy": t.strategy,
                    "request_url": t.request_url,
                }
                for t in self.transforms
            ],
            "ledger": {
                "entries": self.ledger_entries(),
                "non_randomizable": self.non_randomizable_routes(),
            },
            "non_randomizable": self.non_randomizable_routes(),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "DataRandomizationMiddleware":
        """Rebuild middleware from serialized harvest / Run 2 state."""
        mw = cls()
        if not data:
            return mw
        for item in data.get("transforms") or []:
            t = PayloadTransform(
                field_path=str(item.get("field_path") or ""),
                run1_value=str(item.get("run1_value") or ""),
                location=str(item.get("location") or "body"),
                method=str(item.get("method") or "POST").upper(),
                url_path=str(item.get("url_path") or "/"),
                strategy=str(item.get("strategy") or STRATEGY_SUFFIX),
                run2_value=item.get("run2_value"),
                request_url=str(item.get("request_url") or ""),
            )
            mw._register_transform(t)
            if t.run2_value:
                mw._value_map[t.run1_value] = str(t.run2_value)
        for item in data.get("non_randomizable") or []:
            mw._non_randomizable.append(
                NonRandomizableRoute(
                    method=str(item.get("method") or "POST").upper(),
                    url=str(item.get("url") or ""),
                    url_path=str(item.get("url_path") or "/"),
                    reason=str(item.get("reason") or "non_randomizable"),
                    handling=str(item.get("handling") or "mock_response"),
                )
            )
        ledger = data.get("ledger")
        if isinstance(ledger, list):
            mw._ledger = list(ledger)
        elif isinstance(ledger, dict):
            mw._ledger = list(ledger.get("entries") or [])
        if not mw._ledger:
            mw._materialize_replacements()
        else:
            # Ensure transforms carry run2 values from ledger
            by_key = {
                (
                    e.get("method"),
                    e.get("url_path"),
                    e.get("field_path"),
                    e.get("run1_value"),
                ): e
                for e in mw._ledger
            }
            for t in mw.transforms:
                e = by_key.get((t.method, t.url_path, t.field_path, t.run1_value))
                if e and e.get("run2_value"):
                    t.run2_value = str(e["run2_value"])
                    mw._value_map[t.run1_value] = t.run2_value
        return mw

    # Back-compat alias
    from_harvested_dict = from_dict

    @property
    def non_randomizable(self) -> List[NonRandomizableRoute]:
        """Harvested non-randomizable routes (attribute form used by graph logs)."""
        return self._non_randomizable

    @property
    def ledger(self) -> "_LedgerView":
        """Ledger view exposing ``.to_dict()`` / ``.entries`` for graph helpers."""
        return _LedgerView(self.ledger_entries(), self.non_randomizable_routes())


class _LedgerView:
    """Thin adapter so callers can use ``mw.ledger.to_dict()`` or ``.entries``."""

    def __init__(
        self,
        entries: List[Dict[str, Any]],
        non_randomizable: List[Dict[str, Any]],
    ) -> None:
        self.entries = entries
        self.non_randomizable = non_randomizable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": list(self.entries),
            "non_randomizable": list(self.non_randomizable),
        }


def build_middleware_from_run1(
    network_requests: Sequence[Dict[str, Any]],
) -> DataRandomizationMiddleware:
    """Harvest Run 1 captures and materialize Run 2 replacements."""
    mw = DataRandomizationMiddleware()
    mw.harvest_from_requests(network_requests)
    return mw


def _coerce_ledger_entries(ledger: Any) -> List[Dict[str, Any]]:
    if not ledger:
        return []
    if isinstance(ledger, list):
        return list(ledger)
    if isinstance(ledger, dict):
        if isinstance(ledger.get("entries"), list):
            return list(ledger["entries"])
    return []


def is_deliberate_randomization(
    *,
    run1_value: str,
    run2_value: str,
    field_path: str = "",
    ledger: Optional[Sequence[Dict[str, Any]]] = None,
) -> bool:
    """Return True when a Run1/Run2 diff is intentional test-data randomization."""
    if not ledger:
        return False
    a = str(run1_value or "")
    b = str(run2_value or "")
    for e in ledger:
        if str(e.get("run1_value") or "") == a and str(e.get("run2_value") or "") == b:
            return True
        if field_path and str(e.get("field_path") or "") == field_path:
            if str(e.get("run1_value") or "") == a:
                return True
    if a and b and a != b:
        for e in ledger:
            if str(e.get("run1_value") or "") == a and str(e.get("run2_value") or "") == b:
                return True
    return False


def filter_randomized_correlations(
    correlations: List[Dict[str, Any]],
    ledger: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Drop correlation candidates that are deliberate payload randomizations."""
    if not ledger or not correlations:
        return correlations
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for corr in correlations:
        field_path = str(corr.get("json_path") or corr.get("key") or "")
        loc = (corr.get("location") or "").lower()
        if loc == "body" and field_path and not field_path.startswith("payload."):
            cand_path = f"payload.{field_path}"
        elif loc == "query" and field_path and not field_path.startswith("query."):
            cand_path = f"query.{field_path}"
        else:
            cand_path = field_path

        if is_deliberate_randomization(
            run1_value=str(corr.get("run1_value") or ""),
            run2_value=str(corr.get("run2_value") or ""),
            field_path=cand_path,
            ledger=ledger,
        ):
            dropped += 1
            continue
        kept.append(corr)
    if dropped:
        logger.info(
            "Correlation filter: dropped %s deliberate randomization candidate(s).",
            dropped,
        )
    return kept


def filter_randomized_dependencies(
    dependencies: List[Dict[str, Any]],
    ledger: Optional[Sequence[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Drop extract→pass edges whose values were client-side randomized."""
    if not ledger or not dependencies:
        return dependencies
    run1_seeds = {str(e.get("run1_value") or "") for e in ledger if e.get("run1_value")}
    kept: List[Dict[str, Any]] = []
    for dep in dependencies:
        if is_deliberate_randomization(
            run1_value=str(dep.get("run1_value") or ""),
            run2_value=str(dep.get("run2_value") or ""),
            field_path=str(dep.get("target_location") or dep.get("value_key") or ""),
            ledger=ledger,
        ):
            continue
        seed = str(dep.get("run1_value") or "")
        if seed in run1_seeds:
            key = str(dep.get("value_key") or "").lower()
            tgt = str(dep.get("target_location") or "")
            if is_randomizable_field(key) or is_randomizable_field(tgt):
                continue
        kept.append(dep)
    return kept


def filter_correlations_against_ledger(
    correlations: List[Dict[str, Any]],
    ledger: Any = None,
) -> List[Dict[str, Any]]:
    """Accept ledger list or ``{entries: [...]}`` dict (graph compatibility)."""
    return filter_randomized_correlations(correlations, _coerce_ledger_entries(ledger))


def filter_dependencies_against_ledger(
    dependencies: List[Dict[str, Any]],
    ledger: Any = None,
) -> List[Dict[str, Any]]:
    """Accept ledger list or ``{entries: [...]}`` dict (graph compatibility)."""
    return filter_randomized_dependencies(dependencies, _coerce_ledger_entries(ledger))


def apply_randomization_to_ir(
    ir: Dict[str, Any],
    *,
    ledger: Any = None,
    non_randomizable: Optional[Sequence[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Attach the ledger and flag non-randomizable request nodes in the IR.

    Flagged nodes (``requires_manual_data`` / ``mock_in_load_test``) inform the
    deterministic k6 compiler to emit mocks or require CSV provisioning.
    """
    if not ir:
        return ir
    ledger_entries = _coerce_ledger_entries(ledger)
    if non_randomizable is None and isinstance(ledger, dict):
        non_randomizable = list(ledger.get("non_randomizable") or [])
    non_randomizable = list(non_randomizable or [])
    ir = dict(ir)
    ir["randomization_ledger"] = ledger_entries
    ir["non_randomizable_endpoints"] = non_randomizable

    non_rand_paths = {
        ((n.get("method") or "").upper(), n.get("url_path") or "")
        for n in non_randomizable
    }
    non_rand_urls = {n.get("url") for n in non_randomizable if n.get("url")}
    reason_by_key = {
        ((n.get("method") or "").upper(), n.get("url_path") or ""): n.get("reason")
        for n in non_randomizable
    }

    randomized_values = {
        str(e.get("run1_value") or "") for e in ledger_entries if e.get("run1_value")
    }
    strategy_by_value = {
        str(e.get("run1_value") or ""): e.get("strategy") or STRATEGY_SUFFIX
        for e in ledger_entries
        if e.get("run1_value")
    }

    for var in ir.get("vars") or []:
        if not isinstance(var, dict):
            continue
        val = str(var.get("value") or "")
        if val in randomized_values:
            var["randomize"] = True
            var["randomize_strategy"] = strategy_by_value.get(val, STRATEGY_SUFFIX)

    for txn in ir.get("transactions") or []:
        for req in txn.get("requests") or []:
            if not isinstance(req, dict):
                continue
            method = (req.get("method") or "GET").upper()
            url = req.get("url") or ""
            try:
                path = urlparse(re.sub(r"\$\{[^}]+\}", "x", url)).path or "/"
            except Exception:
                path = "/"

            reason = reason_by_key.get((method, path))
            if not reason and ((method, path) in non_rand_paths or url in non_rand_urls):
                reason = "non_randomizable_endpoint"
            if not reason:
                reason = classify_non_randomizable_url(
                    re.sub(r"\$\{[^}]+\}", "placeholder", url), method
                )

            if reason:
                req["requires_manual_data"] = True
                req["non_randomizable"] = True
                req["mock_in_load_test"] = True
                req["manual_data_reason"] = reason
                req["ir_flag"] = "manual_test_data_or_mock"
            else:
                fields = [
                    e.get("field_path")
                    for e in ledger_entries
                    if (e.get("method") or "").upper() == method
                    and e.get("url_path") == path
                ]
                if fields:
                    req["randomized_fields"] = list(dict.fromkeys(fields))
    return ir
