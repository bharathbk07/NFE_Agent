"""
Deterministic k6 emitter.

Takes Load-Test IR (from load_test_ir.build_load_test_ir) and produces a k6 script.
No LLM. Same IR → same script.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.utils.load_test_ir import build_load_test_ir


def _js_string(value: Any) -> str:
    """Encode a value as a JSON-quoted JavaScript string literal.

    Args:
        value: Value to stringify; ``None`` becomes an empty string.

    Returns:
        JavaScript string expression.
    """
    return json.dumps("" if value is None else str(value))


def _safe_ident(name: str, fallback: str = "value") -> str:
    """Normalize arbitrary text into a JavaScript identifier.

    Args:
        name: Desired identifier.
        fallback: Replacement or prefix for invalid names.

    Returns:
        Identifier containing only letters, digits, and underscores.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name or "").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    return cleaned


def _resolve_var_expr(value: Any) -> str:
    """Convert an IR scalar or placeholder into a JavaScript expression.

    Args:
        value: IR scalar, including exact ``${name}`` placeholders.

    Returns:
        JavaScript literal, ``null``, or ``vars.name`` expression.
    """
    if value is None:
        return "null"
    if isinstance(value, (int, float, bool)):
        return json.dumps(value)
    s = str(value)
    m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", s)
    if m:
        return f"vars.{m.group(1)}"
    return _js_string(s)


def _template_js(text: str) -> str:
    """Emit a JS string or template literal when ``${var}`` placeholders exist."""
    s = str(text or "")
    if "${" not in s:
        return _js_string(s)

    def repl(match: re.Match) -> str:
        return f"${{vars.{match.group(1)}}}"

    tmpl = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, s)
    tmpl = tmpl.replace("\\", "\\\\").replace("`", "\\`")
    return f"`{tmpl}`"


def _body_to_js(body: Any, body_type: str) -> Tuple[str, Optional[str]]:
    """Render an IR body as a JavaScript expression.

    Args:
        body: Parsed request body containing optional placeholders.
        body_type: IR body classification such as ``json`` or ``form``.

    Returns:
        Pair of JavaScript expression and optional Content-Type override.
    """
    if body is None or body_type in ("empty", ""):
        return "null", None

    if body_type == "form" and isinstance(body, dict):
        # Pass a JS object — k6 form-encodes when Content-Type is urlencoded
        fields = []
        for k, v in body.items():
            fields.append(f"    {_js_string(k)}: {_resolve_var_expr(v)}")
        obj = "{\n" + ",\n".join(fields) + "\n  }"
        return obj, "application/x-www-form-urlencoded"

    if isinstance(body, (dict, list)):
        # JSON with possible ${var} leaves — rebuild as JS object literal
        def render(node: Any) -> str:
            """Recursively render JSON-compatible data as JavaScript syntax."""
            if isinstance(node, dict):
                parts = [f"{_js_string(k)}: {render(v)}" for k, v in node.items()]
                return "{ " + ", ".join(parts) + " }"
            if isinstance(node, list):
                return "[ " + ", ".join(render(v) for v in node) + " ]"
            return _resolve_var_expr(node)

        return f"JSON.stringify({render(body)})", "application/json"

    return _resolve_var_expr(body), None


def _url_path_signature(url: str) -> str:
    """Path signature with ``${var}`` and numeric ID segments wildcarded."""
    try:
        path = urlparse(url or "").path
    except Exception:
        path = url or ""
    segs = []
    for s in path.split("/"):
        if not s:
            continue
        if s.isdigit() or re.fullmatch(r"\$\{[^}]+\}", s):
            segs.append("{id}")
        else:
            segs.append(s)
    return "/" + "/".join(segs)


def _url_loose_match(a: str, b: str) -> bool:
    """Match URLs ignoring trailing slashes, query, ``${var}``, and path IDs."""
    if not a or not b:
        return False
    if a.rstrip("/") == b.rstrip("/"):
        return True
    # Strip query for structural compare; correlated IDs live in path/query.
    try:
        pa = urlparse(re.sub(r"\$\{[^}]+\}", "X", a))
        pb = urlparse(re.sub(r"\$\{[^}]+\}", "X", b))
        if pa.netloc == pb.netloc and pa.path.rstrip("/") == pb.path.rstrip("/"):
            return True
        # Path IDs: /employees/88 vs /employees/${employees}
        if pa.netloc == pb.netloc and _url_path_signature(a) == _url_path_signature(b):
            return True
    except Exception:
        return False
    return False


def _extract_snippets_for_txn(
    txn_name: str,
    correlations: List[Dict[str, Any]],
    request_urls: List[str],
    *,
    res_var: str = "res",
) -> List[str]:
    """Generate extraction statements for matching response sources."""
    lines: List[str] = []
    for c in correlations:
        if c.get("auto_cookie"):
            continue
        src = c.get("extract") or {}
        from_req = src.get("from_request") or ""
        if not from_req:
            continue
        if not any(_url_loose_match(from_req, u) for u in request_urls):
            continue
        var = _safe_ident(c.get("var") or "token", "token")
        loc = str(src.get("from_location") or "")
        if loc.startswith("body.regex:"):
            pattern = loc[len("body.regex:") :]
            lines.append(f"    // Correlation extract `{var}` via HTML/body regex")
            # Support patterns with one or two capturing groups (attr order variants)
            lines.append(
                f"    {{ const __m = String({res_var}.body || '').match("
                f"new RegExp({_js_string(pattern)})); "
                f"if (__m) vars.{var} = __m[1] || __m[2] || vars.{var}; }}"
            )
        elif loc.startswith("body.$") or loc.startswith("body."):
            path = loc[len("body.") :] if loc.startswith("body.") else loc
            jp = path.lstrip("$.").replace(".", ".")
            if jp.startswith("$"):
                jp = jp[1:].lstrip(".")
            lines.append(f"    // Correlation extract `{var}` from {loc}")
            lines.append(
                f"    vars.{var} = {res_var}.json({_js_string(jp)}) || vars.{var};"
            )
        elif "set-cookie" in loc.lower():
            lines.append(
                f"    // Cookie `{var}` managed by k6 cookie jar (from {loc})"
            )
        elif "ui." in loc.lower():
            lines.append(
                f"    // UI correlation `{var}`: read from page after submit/create"
            )
        else:
            lines.append(
                f"    // TODO extract `{var}` from {loc} on {from_req}"
            )
    return lines


def _runtime_helpers_js() -> str:
    """Inline NFE metrics + assertion helpers (no external imports)."""
    path = Path(__file__).with_name("k6_runtime_helpers.js")
    return path.read_text(encoding="utf-8").strip()


def _expect_json(headers: Dict[str, Any], body_type: str) -> bool:
    """Return True when the response is expected to be JSON."""
    if (body_type or "").lower() in ("json",):
        return True
    for k, v in (headers or {}).items():
        if str(k).lower() == "accept" and "json" in str(v).lower():
            return True
        if str(k).lower() == "content-type" and "json" in str(v).lower():
            return True
    return False


def _emit_protocol_txn(txn: Dict[str, Any], correlations: List[Dict[str, Any]]) -> str:
    """Emit one protocol-mode k6 transaction function.

    Args:
        txn: IR transaction with requests and timing metadata.
        correlations: IR correlations used to place extraction statements.

    Returns:
        JavaScript function source.
    """
    name = txn["name"]
    desc = txn.get("description") or name
    think = txn.get("think_time_s", 1)
    reqs = txn.get("requests") or []
    comments = "\n".join(
        f"    // - {r.get('method')} {r.get('url')}" for r in reqs[:30]
    ) or "    // (no requests)"

    body_lines: List[str] = [
        f"    const __nfeTxnStart = Date.now();",
    ]
    urls = [r.get("url") or "" for r in reqs]

    for i, r in enumerate(reqs):
        method = (r.get("method") or "GET").upper()
        url_js = _template_js(r.get("url") or "")
        var = "res" if i == 0 else f"res{i}"
        body_js, ct = _body_to_js(r.get("body"), r.get("body_type") or "empty")
        headers = dict(r.get("headers") or {})
        if ct and "content-type" not in {h.lower() for h in headers}:
            headers["Content-Type"] = ct

        header_parts = [
            f"{_js_string(k)}: {_template_js(v) if '${' in str(v) else _js_string(v)}"
            for k, v in headers.items()
        ]
        headers_js = "{ " + ", ".join(header_parts) + " }" if header_parts else "{}"

        # name tag groups URL metrics in k6; txn links rows in the HTML report
        name_tag = f"{method} " + (r.get("url") or "")
        if len(name_tag) > 180:
            name_tag = name_tag[:177] + "..."
        tags_js = (
            "{ "
            f"txn: {_js_string(name)}, "
            f"method: {_js_string(method)}, "
            f"name: {_js_string(name_tag)} "
            "}"
        )
        params_bits = [f"headers: {headers_js}", f"tags: {tags_js}"]
        if r.get("redirects") is not None:
            params_bits.append(f"redirects: {int(r.get('redirects'))}")
        elif method == "POST" and "/auth/" in str(r.get("url") or "").lower():
            params_bits.append("redirects: 5")
        params_obj = "{ " + ", ".join(params_bits) + " }"

        if method == "GET":
            body_lines.append(f"    const {var} = http.get({url_js}, {params_obj});")
        elif method == "POST":
            body_lines.append(
                f"    const {var} = http.post({url_js}, {body_js}, {params_obj});"
            )
        elif method == "PUT":
            body_lines.append(
                f"    const {var} = http.put({url_js}, {body_js}, {params_obj});"
            )
        elif method == "PATCH":
            body_lines.append(
                f"    const {var} = http.patch({url_js}, {body_js}, {params_obj});"
            )
        elif method == "DELETE":
            body_lines.append(
                f"    const {var} = http.del({url_js}, {body_js}, {params_obj});"
            )
        else:
            body_lines.append(
                f"    const {var} = http.request({_js_string(method)}, {url_js}, {body_js}, {params_obj});"
            )

        soft = "true" if r.get("soft_check") else "false"
        expect_json = "true" if _expect_json(headers, str(r.get("body_type") or "")) else "false"
        body_lines.append(
            f"    nfeAssertResponse({var}, {_js_string(name)}, {_js_string(method)}, "
            f"{{ soft: {soft}, expectJson: {expect_json}, label: {url_js} }});"
        )

        raw_url = r.get("url") or ""
        body_lines.extend(
            _extract_snippets_for_txn(
                name, correlations, [raw_url], res_var=var
            )
        )

    body_lines.append(f"    nfeMarkTxn({_js_string(name)}, __nfeTxnStart);")
    body_lines.append(f"    sleep({think});")

    if len(body_lines) <= 2:
        body_lines = [
            "    // No protocol HTTP for this TXN — check browser mode",
            f"    nfeMarkTxn({_js_string(name)}, Date.now());",
            f"    sleep({think});",
        ]

    return f"""
export function {name}() {{
  // TXN: {desc}
  // Mode: protocol (from IR)
  // Requests:
{comments}
  group({_js_string(name)}, function () {{
{chr(10).join(body_lines)}
  }});
}}""".rstrip()


def _emit_browser_txn(txn: Dict[str, Any], target_url: str) -> str:
    """Emit one browser-mode k6 transaction function.

    Args:
        txn: IR transaction containing normalized UI steps.
        target_url: Fallback navigation URL.

    Returns:
        Async JavaScript function source.
    """
    name = txn["name"]
    desc = txn.get("description") or name
    think = txn.get("think_time_s", 1)
    ui_steps = txn.get("ui_steps") or []
    reqs = txn.get("requests") or []
    seed = (reqs[0]["url"] if reqs else target_url) or "about:blank"

    lines: List[str] = []
    for step in ui_steps:
        action = step.get("action")
        selector = step.get("selector") or ""
        value = step.get("value")
        url = step.get("url") or ""
        if action == "navigate" and url:
            lines.append(f"    await page.goto({_js_string(url)});")
        elif action == "fill" and selector:
            lines.append(
                f"    await page.locator({_js_string(selector)}).type({_resolve_var_expr(value)});"
            )
        elif action == "click" and selector:
            lines.append(f"    await page.locator({_js_string(selector)}).click();")
        elif action == "select" and selector:
            lines.append(
                f"    await page.locator({_js_string(selector)}).selectOption({_resolve_var_expr(value)});"
            )
        elif action == "wait_for_selector" and selector:
            lines.append(f"    await page.locator({_js_string(selector)}).waitFor();")
        elif action in ("wait", "wait_for_load"):
            lines.append("    await page.waitForLoadState('networkidle');")

    body = "\n".join(lines) if lines else "    // (no UI steps)"
    comments = "\n".join(
        f"    // - UI {s.get('action')} {s.get('selector') or s.get('url') or ''}".rstrip()
        for s in ui_steps[:20]
    ) or "    // (no UI)"

    return f"""
export async function {name}(page) {{
  // TXN: {desc}
  // Mode: browser (SPA / UI-driven — from IR)
{comments}
  // Note: k6 group() does not support async callbacks — mark TXN timing manually.
  const __nfeTxnStart = Date.now();
  if (!page.url() || page.url() === 'about:blank') {{
    await page.goto({_js_string(seed)});
  }}
{body}
  nfeMarkTxn({_js_string(name)}, __nfeTxnStart);
  await sleep({think});
}}""".rstrip()


def emit_k6_from_ir(ir: Dict[str, Any]) -> str:
    """Deterministically emit k6 JavaScript from Load-Test IR.

    Args:
        ir: IR mapping with variables, correlations, and transactions.

    Returns:
        Complete protocol or browser-enabled k6 JavaScript source.
    """
    target_url = ir.get("target_url") or ""
    host = urlparse(target_url).netloc or "example.com"
    vars_list = ir.get("vars") or []
    correlations = ir.get("correlations") or []
    transactions = ir.get("transactions") or []

    # Preserve IR insertion order so identical IR emits byte-for-byte.
    param_block = ",\n".join(
        f"  {v['name']}: {_js_string(v.get('value'))}" for v in vars_list
    ) or "  // no parameters detected"

    # Mutable bag for runtime correlation extracts (unique names only)
    corr_var_decls = []
    seen_var_names = {v["name"] for v in vars_list}
    for c in correlations:
        if c.get("auto_cookie"):
            continue
        var = _safe_ident(c.get("var") or "token", "token")
        if var in seen_var_names:
            continue
        seen_var_names.add(var)
        corr_var_decls.append(f"  {var}: '', // filled by correlation extract")

    if corr_var_decls:
        if param_block.startswith("  //"):
            param_block = ",\n".join(corr_var_decls)
        else:
            param_block = param_block + ",\n" + ",\n".join(corr_var_decls)

    corr_comments = []
    for c in correlations:
        var = c.get("var")
        ex = c.get("extract") or {}
        ps = c.get("pass") or {}
        if c.get("auto_cookie"):
            corr_comments.append(
                f"  // Cookie `{var}`: extract {ex.get('from_location')} → "
                f"pass {ps.get('to_location')} (k6 cookie jar handles this)"
            )
        else:
            corr_comments.append(
                f"  // `{var}` [{c.get('confidence')}]: "
                f"{ex.get('from_location')} @ {ex.get('from_request')} → "
                f"{ps.get('to_location')} @ {ps.get('to_request')}"
            )
    corr_block = (
        "\n".join(corr_comments)
        if corr_comments
        else "  // No traced correlations — cookie jar / session may still apply."
    )

    cookie_notes = ir.get("cookie_notes") or []
    cookie_lines = []
    for n in cookie_notes[:20]:
        if not isinstance(n, dict):
            continue
        name = n.get("cookie_name") or "?"
        must = "REQUIRED" if n.get("must_correlate") else "verify"
        conf = n.get("confidence") or "uncertain"
        cookie_lines.append(
            f"  // Cookie `{name}` [{must}/{conf}]: {n.get('note') or ''}"
        )
    if cookie_lines:
        corr_block = corr_block + "\n" + "\n".join(cookie_lines)
    elif not cookie_notes:
        corr_block = (
            corr_block
            + "\n  // Tip: persist cookies after login; many apps rely on session cookies."
        )

    needs_browser = False
    txn_fns: List[str] = []
    txn_meta: List[Tuple[str, bool]] = []

    for txn in transactions:
        mode = txn.get("mode") or "protocol"
        if mode == "browser":
            needs_browser = True
            txn_fns.append(_emit_browser_txn(txn, target_url))
            txn_meta.append((txn["name"], True))
        else:
            if not txn.get("requests"):
                # fallback single GET to avoid empty fn
                txn = {
                    **txn,
                    "requests": [
                        {
                            "method": "GET",
                            "url": target_url or f"https://{host}/",
                            "headers": {},
                            "body": None,
                            "body_type": "empty",
                        }
                    ],
                }
            txn_fns.append(_emit_protocol_txn(txn, correlations))
            txn_meta.append((txn["name"], False))

    if not txn_fns:
        txn_fns.append(
            f"""
export function Launch() {{
  group('Launch', function () {{
    const __nfeTxnStart = Date.now();
    const res = http.get({_js_string(target_url or f'https://{host}/')}, {{
      tags: {{ txn: 'Launch', method: 'GET', name: 'GET launch' }},
    }});
    nfeAssertResponse(res, 'Launch', 'GET', {{ soft: false, expectJson: false }});
    nfeMarkTxn('Launch', __nfeTxnStart);
    sleep(1);
  }});
}}""".rstrip()
        )
        txn_meta.append(("Launch", False))

    if needs_browser:
        call_lines = []
        for name, is_browser in txn_meta:
            if is_browser:
                call_lines.append(f"  await {name}(page);")
                # After browser login, copy cookies into the http module jar
                txn_obj = next((t for t in transactions if t.get("name") == name), {})
                if txn_obj.get("sync_cookies_to_http") or name.lower() == "login":
                    call_lines.append(
                        "  // Sync browser session cookies → http jar for protocol TXNs\n"
                        "  {\n"
                        "    const jar = http.cookieJar();\n"
                        "    const cookies = await page.context().cookies();\n"
                        "    for (const c of cookies) {\n"
                        "      const host = (c.domain || '').replace(/^\\./, '');\n"
                        "      if (!host || !c.name) continue;\n"
                        "      const base = `${c.secure ? 'https' : 'http'}://${host}${c.path || '/'}`;\n"
                        "      try { jar.set(base, c.name, c.value); } catch (e) {}\n"
                        "    }\n"
                        "  }"
                    )
            else:
                call_lines.append(f"  {name}();")
        calls = "\n".join(call_lines)
        return f"""import http from 'k6/http';
import {{ browser }} from 'k6/browser';
import {{ check, group, sleep }} from 'k6';
import {{ Trend, Counter }} from 'k6/metrics';

/**
 * Auto-generated by NFE Agent from Load-Test IR (deterministic — no LLM).
 * Target: {target_url}
 * IR version: {ir.get('version', 1)}
 * Hybrid: browser login (SPA CSRF) + protocol API TXNs.
 */

export const options = {{
  scenarios: {{
    smoke: {{
      executor: 'shared-iterations',
      vus: 1,
      iterations: 2,
      maxDuration: '2m',
      options: {{ browser: {{ type: 'chromium' }} }},
    }},
  }},
  summaryTrendStats: ['min', 'avg', 'med', 'max', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'count'],
  // SLA thresholds (k6-learn): error rate, p95 response time, checks
  thresholds: {{
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
    checks: ['rate>0.99'],
  }},
}};

{_runtime_helpers_js()}

const vars = {{
{param_block}
}};

{chr(10).join(txn_fns)}

export default async function () {{
  // Correlation checklist:
{corr_block}

  const page = await browser.newPage();
  try {{
{calls}
  }} finally {{
    await page.close();
  }}
}}

{_handle_summary_block()}
"""

    calls = "\n".join(f"  {name}();" for name, _ in txn_meta)
    return f"""import http from 'k6/http';
import {{ check, group, sleep }} from 'k6';
import {{ Trend, Counter }} from 'k6/metrics';

/**
 * Auto-generated by NFE Agent from Load-Test IR (deterministic — no LLM).
 * Target: {target_url}
 * IR version: {ir.get('version', 1)}
 *
 * Protocol TXNs replay captured METHOD+URL (+ body when available).
 * Cookie-based sessions use the k6 cookie jar automatically.
 * Each response is asserted (status / body / optional JSON) and recorded
 * into tagged metrics for the HTML report tables.
 */

export const options = {{
  scenarios: {{
    smoke: {{
      executor: 'shared-iterations',
      vus: 1,
      iterations: 2,
      maxDuration: '2m',
    }},
  }},
  summaryTrendStats: ['min', 'avg', 'med', 'max', 'p(50)', 'p(90)', 'p(95)', 'p(99)', 'count'],
  // SLA thresholds (k6-learn): error rate, p95 response time, checks
  thresholds: {{
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
    checks: ['rate>0.99'],
  }},
}};

{_runtime_helpers_js()}

const vars = {{
{param_block}
}};

{chr(10).join(txn_fns)}

export default function () {{
  // Correlation checklist:
{corr_block}

{calls}
}}

{_handle_summary_block()}
"""


def _handle_summary_block() -> str:
    """Return embedded ``handleSummary`` source for the HTML report."""
    from src.utils.k6_html_report import load_handle_summary_js

    return (
        "// --- NFE HTML report (handleSummary) ---\n"
        + load_handle_summary_js()
    )


def generate_k6_script(
    *,
    target_url: str,
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
    network_requests: Optional[List[Dict[str, Any]]] = None,
    ir: Optional[Dict[str, Any]] = None,
) -> str:
    """Build IR when needed and return deterministic k6 source.

    Args:
        target_url: Journey target URL.
        parameterizable_candidates: User-fed parameter candidates.
        dependencies: Extract-to-pass correlation edges.
        transactions: Transaction definitions.
        network_requests: Optional captures used to enrich newly built IR.
        ir: Optional pre-built Load-Test IR.

    Returns:
        Complete k6 JavaScript source string.
    """
    if ir is None:
        ir = build_load_test_ir(
            target_url=target_url,
            parameterizable_candidates=parameterizable_candidates,
            dependencies=dependencies,
            transactions=transactions,
            network_requests=network_requests,
        )
    return emit_k6_from_ir(ir)
