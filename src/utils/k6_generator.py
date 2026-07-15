"""Generate a starter k6 script from analysis results (params, correlations, TXNs)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse


def _js_string(value: Any) -> str:
    return json.dumps("" if value is None else str(value))


def _safe_ident(name: str, fallback: str = "value") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]", "_", name or "").strip("_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"{fallback}_{cleaned}" if cleaned else fallback
    return cleaned


def _normalize_url(url: str, default_origin: str = "") -> str:
    u = (url or "").strip().rstrip("`").strip()
    if not u:
        return ""
    if u.startswith("http://") or u.startswith("https://"):
        return u
    # Labels like "www.saucedemo.com/inventory.html"
    if default_origin and u.startswith("/"):
        return default_origin.rstrip("/") + u
    if "://" not in u and "." in u.split("/")[0]:
        scheme = urlparse(default_origin).scheme or "https"
        return f"{scheme}://{u.lstrip('/')}"
    if default_origin and u.startswith("/"):
        return default_origin.rstrip("/") + u
    return u


def _parse_request_label(label: str, default_origin: str = "") -> Optional[Dict[str, str]]:
    """Parse 'GET https://host/path' or 'POST host/path' into method+url."""
    text = (label or "").strip()
    if not text or text.startswith("UI ") or text.startswith("("):
        return None
    m = re.match(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(\S+)$", text, re.I)
    if not m:
        # Bare URL
        if text.startswith("http://") or text.startswith("https://"):
            return {"method": "GET", "url": text}
        return None
    method = m.group(1).upper()
    raw = m.group(2)
    url = _normalize_url(raw, default_origin)
    if not url.startswith("http"):
        return None
    return {"method": method, "url": url}


def _http_entries_for_txn(
    txn: Dict[str, Any],
    target_url: str,
) -> List[Dict[str, str]]:
    """Prefer structured http_entries; else parse labels."""
    origin = ""
    try:
        p = urlparse(target_url or "")
        if p.scheme and p.netloc:
            origin = f"{p.scheme}://{p.netloc}"
    except Exception:
        origin = ""

    entries: List[Dict[str, str]] = []
    seen = set()

    for e in txn.get("http_entries") or []:
        if not isinstance(e, dict):
            continue
        method = (e.get("method") or "GET").upper()
        url = _normalize_url(str(e.get("url") or ""), origin)
        if not url.startswith("http"):
            continue
        key = (method, url)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"method": method, "url": url})

    if entries:
        return entries

    for label in txn.get("http_requests") or txn.get("request_urls") or []:
        parsed = _parse_request_label(str(label), origin)
        if not parsed:
            continue
        key = (parsed["method"], parsed["url"])
        if key in seen:
            continue
        seen.add(key)
        entries.append(parsed)

    return entries


def _ui_steps_for_txn(txn: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps = txn.get("ui_steps") or []
    if steps:
        return [s for s in steps if isinstance(s, dict)]
    # Best-effort parse from ui_actions labels
    out: List[Dict[str, Any]] = []
    for line in txn.get("ui_actions") or []:
        text = str(line)
        if text.startswith("UI fill "):
            # UI fill #user-name = standard_user
            m = re.match(r"UI fill\s+(\S+)\s*=\s*(.*)$", text)
            if m:
                out.append({"action": "fill", "selector": m.group(1), "value": m.group(2)})
        elif text.startswith("UI click "):
            out.append({"action": "click", "selector": text[len("UI click ") :].strip()})
        elif text.startswith("UI navigate"):
            m = re.search(r"→\s*(\S+)", text)
            if m:
                out.append({"action": "navigate", "url": m.group(1)})
        elif "wait_for_selector" in text:
            m = re.search(r"wait_for_selector\s+(\S+)", text)
            if m:
                out.append({"action": "wait_for_selector", "selector": m.group(1)})
        elif text.startswith("UI wait_for_load") or text == "UI wait_for_load":
            out.append({"action": "wait_for_load"})
    return out


def _emit_http_calls(entries: List[Dict[str, str]], txn_name: str) -> str:
    lines: List[str] = []
    for i, e in enumerate(entries):
        method = e["method"]
        url_js = _js_string(e["url"])
        tag = f"{{ tags: {{ txn: {_js_string(txn_name)} }} }}"
        var = "res" if i == 0 else f"res{i}"
        if method == "GET":
            lines.append(f"    const {var} = http.get({url_js}, {tag});")
        elif method == "POST":
            lines.append(
                f"    const {var} = http.post({url_js}, null, {tag}); "
                f"// TODO: add form body / JSON from recording"
            )
        elif method == "PUT":
            lines.append(f"    const {var} = http.put({url_js}, null, {tag});")
        elif method == "PATCH":
            lines.append(f"    const {var} = http.patch({url_js}, null, {tag});")
        elif method == "DELETE":
            lines.append(f"    const {var} = http.del({url_js}, null, {tag});")
        else:
            lines.append(
                f"    const {var} = http.request({_js_string(method)}, {url_js}, null, {tag});"
            )
        lines.append(
            f"    check({var}, {{ '{txn_name} {method} {i+1} is 2xx': "
            f"(r) => r.status >= 200 && r.status < 300 }});"
        )
    if not lines:
        return "    // No protocol-level HTTP captured for this TXN"
    return "\n".join(lines)


def _emit_browser_actions(ui_steps: List[Dict[str, Any]], params: Dict[str, str]) -> str:
    """Emit k6/browser page actions from Playwright UI steps."""
    lines: List[str] = []
    for step in ui_steps:
        action = step.get("action")
        selector = step.get("selector") or ""
        value = step.get("value")
        url = step.get("url") or ""
        if action == "navigate" and url:
            lines.append(f"    await page.goto({_js_string(url)});")
        elif action == "fill" and selector:
            # Prefer vars.* when value matches a known param
            val_expr = _js_string(value)
            for pname, pval in params.items():
                if str(value) == str(pval):
                    val_expr = f"vars.{pname}"
                    break
            lines.append(f"    await page.locator({_js_string(selector)}).type({val_expr});")
        elif action == "click" and selector:
            lines.append(f"    await page.locator({_js_string(selector)}).click();")
        elif action == "select" and selector:
            lines.append(
                f"    await page.locator({_js_string(selector)}).selectOption({_js_string(value)});"
            )
        elif action == "wait_for_selector" and selector:
            lines.append(f"    await page.locator({_js_string(selector)}).waitFor();")
        elif action in ("wait", "wait_for_load"):
            lines.append("    await page.waitForLoadState('networkidle');")
    return "\n".join(lines) if lines else "    // (no UI steps)"


def generate_k6_script(
    *,
    target_url: str,
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]],
) -> str:
    """
    Produce a k6 script from captured HTTP per TXN.

    - Protocol (http.*) when TXN has real METHOD+URL entries
    - k6 browser module when a TXN is UI-only (SPA client-side actions)
    """
    params: Dict[str, str] = {}
    for cand in parameterizable_candidates or []:
        var = _safe_ident(cand.get("variable_name") or "input")
        if var not in params:
            params[var] = str(cand.get("value") or "")

    corr_lines = []
    seen_corr = set()
    for dep in dependencies or []:
        key = (
            dep.get("value_key"),
            dep.get("source_request"),
            dep.get("target_request"),
        )
        if key in seen_corr:
            continue
        seen_corr.add(key)
        var = _safe_ident(dep.get("value_key") or "token", "token")
        corr_lines.append(
            f"  // Extract `{var}` from {dep.get('source_location')} "
            f"in {dep.get('source_request')}\n"
            f"  // Pass `{var}` into {dep.get('target_location')} "
            f"in {dep.get('target_request')}"
        )

    param_block = ",\n".join(
        f"  {name}: {_js_string(val)}" for name, val in params.items()
    ) or "  // no parameters detected"

    host = urlparse(target_url or "").netloc or "example.com"
    needs_browser = False
    txn_fns: List[str] = []
    txn_meta: List[Tuple[str, bool]] = []  # (name, is_async_browser)

    for txn in transactions or []:
        name = _safe_ident(txn.get("name") or "Txn", "Txn")
        desc = txn.get("description") or name
        entries = _http_entries_for_txn(txn, target_url or "")
        ui_steps = _ui_steps_for_txn(txn)

        req_comments = "\n".join(
            f"    // - {e['method']} {e['url']}" for e in entries[:30]
        )
        if not req_comments and ui_steps:
            req_comments = "\n".join(
                f"    // - UI {s.get('action')} {s.get('selector') or s.get('url') or ''}".rstrip()
                for s in ui_steps[:20]
            )
        if not req_comments:
            req_comments = "    // (no requests)"

        # Prefer protocol when we have captured METHOD+URL entries.
        # Use browser only for UI-driven phases with no useful distinct HTTP.
        use_browser = not entries and bool(ui_steps)
        if (
            entries
            and ui_steps
            and len({e["url"].rstrip("/") for e in entries}) == 1
            and len(ui_steps) >= 2
        ):
            # Same page document only (e.g. add-to-cart on inventory) → browser actions
            use_browser = True
        if (
            entries
            and ui_steps
            and len({e["url"] for e in entries}) == 1
            and entries[0]["url"].rstrip("/") == (target_url or "").rstrip("/")
            and len(ui_steps) >= 2
        ):
            use_browser = True

        if use_browser:
            needs_browser = True
            body = _emit_browser_actions(ui_steps, params)
            # Seed page at first URL if known
            seed = entries[0]["url"] if entries else (target_url or f"https://{host}/")
            txn_fns.append(
                f"""
export async function {name}(page) {{
  // TXN: {desc}
  // Mode: k6 browser (client-side / UI-driven phase)
  // Activity:
{req_comments}
  await group({_js_string(name)}, async function () {{
    if (page.url() === 'about:blank') {{
      await page.goto({_js_string(seed)});
    }}
{body}
    await sleep(1);
  }});
}}""".rstrip()
            )
            txn_meta.append((name, True))
        else:
            if not entries:
                # Last resort: single GET target — marked clearly
                entries = [{"method": "GET", "url": target_url or f"https://{host}/"}]
                req_comments = (
                    f"    // - GET {entries[0]['url']} "
                    "(fallback — no per-phase HTTP captured; re-run journey to enrich)"
                )
            http_body = _emit_http_calls(entries, name)
            txn_fns.append(
                f"""
export function {name}() {{
  // TXN: {desc}
  // Mode: protocol (captured HTTP)
  // Requests under this transaction:
{req_comments}
  group({_js_string(name)}, function () {{
{http_body}
    sleep(1);
  }});
}}""".rstrip()
            )
            txn_meta.append((name, False))

    if not txn_fns:
        txn_fns.append(
            f"""
export function Launch() {{
  group('Launch', function () {{
    const res = http.get({_js_string(target_url or f'https://{host}/')}, {{ tags: {{ txn: 'Launch' }} }});
    check(res, {{ 'Launch ok': (r) => r.status >= 200 && r.status < 400 }});
    sleep(1);
  }});
}}""".rstrip()
        )
        txn_meta.append(("Launch", False))

    corr_block = (
        "\n".join(corr_lines)
        if corr_lines
        else "  // No traced correlations — manage cookies/session if auth is cookie-based."
    )

    if needs_browser:
        call_lines = []
        for name, is_browser in txn_meta:
            if is_browser:
                call_lines.append(f"  await {name}(page);")
            else:
                call_lines.append(f"  {name}();")
        calls = "\n".join(call_lines)
        return f"""import http from 'k6/http';
import {{ browser }} from 'k6/browser';
import {{ check, group, sleep }} from 'k6';

/**
 * Auto-generated by NFE Agent (hybrid protocol + browser)
 * Target: {target_url}
 *
 * Protocol TXNs use captured METHOD+URL sequences.
 * UI-only SPA phases use the k6 browser module (real selectors from Playwright).
 * Run with: k6 run --no-usage-report script.js
 * (Browser scenario requires a k6 build with browser support.)
 */

export const options = {{
  scenarios: {{
    ui: {{
      executor: 'shared-iterations',
      vus: 1,
      iterations: 1,
      options: {{ browser: {{ type: 'chromium' }} }},
    }},
  }},
  thresholds: {{
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
  }},
}};

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
"""

    calls = "\n".join(f"  {name}();" for name, _ in txn_meta)
    return f"""import http from 'k6/http';
import {{ check, group, sleep }} from 'k6';

/**
 * Auto-generated by NFE Agent (protocol mode)
 * Target: {target_url}
 *
 * Each TXN emits the captured METHOD+URL sequence (not a single landing-page GET).
 * Fill in POST bodies / correlation extractors from the Correlations section.
 */

export const options = {{
  vus: 1,
  duration: '30s',
  thresholds: {{
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<2000'],
  }},
}};

const vars = {{
{param_block}
}};

{chr(10).join(txn_fns)}

export default function () {{
  // Correlation checklist:
{corr_block}

{calls}
}}
"""
