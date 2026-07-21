"""Render correlation analysis as Markdown and structured performance-test output."""

import re
from typing import List, Dict, Any, Set
from urllib.parse import urlparse

from src.utils.correlation_noise import (
    is_actionable_correlation,
    is_actionable_dependency,
)
from src.utils.perf_test_classification import looks_like_person_name


def get_step_label(idx: int, user_steps: List[Any]) -> str:
    """Build a human-readable label for a journey step index.

    Args:
        idx: Zero-based step index, with ``-1`` denoting initial navigation.
        user_steps: Ordered journey step dictionaries or displayable values.

    Returns:
        Concise step label string.
    """
    if idx == -1:
        return "Initial Navigation"
    if 0 <= idx < len(user_steps):
        step = user_steps[idx]
        if isinstance(step, dict):
            action = step.get("action", "")
            selector = step.get("selector", "")
            val = step.get("value", "")
            url_val = step.get("url", "")
            if action == "navigate":
                return f"Step {idx + 1}: Navigate to {url_val}"
            elif action == "click":
                return f"Step {idx + 1}: Click `{selector}`"
            elif action == "fill":
                return f"Step {idx + 1}: Fill `{selector}` with `{val}`"
            elif action == "select":
                return f"Step {idx + 1}: Select `{val}` in `{selector}`"
            else:
                return f"Step {idx + 1}: {action} {selector}".strip()
        return f"Step {idx + 1}: {step}"
    return f"Step {idx + 1}"


def _dedupe_dependencies(dependencies: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate correlation edges while preserving their first-seen order.

    Args:
        dependencies: Extract-to-pass dependency dictionaries.

    Returns:
        Dependencies unique by source, target, locations, and value key.
    """
    seen: Set[tuple] = set()
    unique = []
    for dep in dependencies:
        key = (
            dep.get("source_request"),
            dep.get("source_location"),
            dep.get("target_request"),
            dep.get("target_location"),
            dep.get("value_key"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(dep)
    return unique


def _escape_table_cell(value: Any, max_len: int = 120) -> str:
    """Sanitize and bound a Markdown table cell.

    Args:
        value: Arbitrary display value.
        max_len: Maximum returned character count.

    Returns:
        Single-line escaped and optionally truncated text.
    """
    text = str(value or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _uncorrelated_dynamics(
    correlations: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Find dynamic values not covered by an extract-to-pass dependency.

    Args:
        correlations: Dynamic values discovered between captures.
        dependencies: Reconciled extract-to-pass edges.

    Returns:
        Correlation dictionaries with no matching key or observed value pair.
    """
    covered_keys = set()
    covered_values = set()
    for d in dependencies:
        covered_keys.add(
            (d.get("target_request"), d.get("target_location"), d.get("value_key"))
        )
        covered_keys.add(
            (d.get("source_request"), d.get("source_location"), d.get("value_key"))
        )
        covered_values.add(
            (d.get("value_key"), d.get("run1_value"), d.get("run2_value"))
        )

    uncorrelated = []
    for corr in correlations:
        target_loc = f"{corr.get('location')}.{corr.get('key')}"
        key = (corr.get("request_url"), target_loc, corr.get("dynamic_name"))
        val_key = (
            corr.get("dynamic_name"),
            corr.get("run1_value"),
            corr.get("run2_value"),
        )
        if key in covered_keys or val_key in covered_values:
            continue
        uncorrelated.append(corr)
    return uncorrelated


def _api_endpoint_label(url: str) -> str:
    """Turn a long URL into a short API path label for humans."""
    try:
        parsed = urlparse(str(url or ""))
        path = parsed.path or "/"
        # Keep last 3 meaningful segments
        parts = [p for p in path.split("/") if p]
        if len(parts) > 3:
            path = "/" + "/".join(parts[-3:])
        return path
    except Exception:
        return _short_url(url, 40)


def _humanize_propagation(text: str) -> str:
    """Compress a raw propagation string into a short script hint."""
    raw = str(text or "")
    # `POST` Body field `remarks` (json) in `https://...`
    m = re.search(
        r"`?(GET|POST|PUT|PATCH|DELETE)`?\s+"
        r"(?:Body field|Query|Header)\s+`([^`]+)`"
        r"(?:\s+\([^)]*\))?\s+in\s+`([^`]+)`",
        raw,
        re.IGNORECASE,
    )
    if m:
        method, field, url = m.group(1).upper(), m.group(2), m.group(3)
        leaf = field.split(".")[-1]
        return f"{method} {_api_endpoint_label(url)} → {leaf}"
    if "Client-side" in raw or not raw.strip():
        return "Form only (used at submit)"
    return _escape_table_cell(raw, max_len=60)


def _format_parameters_table(parameterizable_candidates: List[Dict[str, Any]]) -> str:
    """Render unique parameter candidates as an end-user Markdown table.

    Args:
        parameterizable_candidates: Parameter dictionaries with selector,
            value, variable, credential, and propagation metadata.

    Returns:
        Markdown table or an empty-state sentence, ending with a newline.
    """
    # Dedupe by variable name — keep the row with the best network evidence.
    best: Dict[str, Dict[str, Any]] = {}
    for cand in parameterizable_candidates or []:
        var_name = str(cand.get("variable_name") or "input_value").strip() or "input_value"
        props = cand.get("propagations") or []
        score = (2 if props else 0) + (1 if cand.get("is_credential") else 0)
        prev = best.get(var_name)
        if prev is None:
            best[var_name] = cand
            continue
        prev_score = (2 if (prev.get("propagations") or []) else 0) + (
            1 if prev.get("is_credential") else 0
        )
        if score > prev_score:
            best[var_name] = cand

    unique_parameterizable = list(best.values())
    if not unique_parameterizable:
        return "_No user inputs to vary were detected._\n"

    lines = [
        "_These are values **you** control in a load test (CSV / data file). "
        "Use the **Data file column** name as the column header._\n",
        "| Data file column | Example value | Sent to API as |",
        "| --- | --- | --- |",
    ]
    for cand in unique_parameterizable:
        var_name = cand.get("variable_name", "input_value")
        propagations = cand.get("propagations") or []
        if propagations:
            where = _humanize_propagation(propagations[0])
            if len(propagations) > 1:
                where += f" (+{len(propagations) - 1} more)"
        else:
            where = "Login / form field"
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(var_name),
                    _escape_table_cell(cand.get("value"), max_len=40),
                    _escape_table_cell(where, max_len=70),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _short_url(url: str, max_len: int = 70) -> str:
    """Compact a URL for table display.

    Args:
        url: URL-like value.
        max_len: Maximum returned character count.

    Returns:
        Original or ellipsis-truncated URL text.
    """
    text = str(url or "")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _group_dependencies_by_value(
    dependencies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Group dependency edges by extracted variable and source.

    Args:
        dependencies: Extract-to-pass dependency dictionaries.

    Returns:
        Group dictionaries containing one extraction source and deduplicated
        ``pass_to`` target lists.
    """
    groups: Dict[tuple, Dict[str, Any]] = {}
    for dep in dependencies:
        key = (
            dep.get("value_key"),
            dep.get("source_request"),
            dep.get("source_location"),
            dep.get("run1_value"),
            dep.get("run2_value"),
        )
        if key not in groups:
            groups[key] = {
                "variable": dep.get("value_key"),
                "run1_value": dep.get("run1_value", ""),
                "run2_value": dep.get("run2_value", ""),
                "extract_request": dep.get("source_request"),
                "extract_location": dep.get("source_location"),
                "extract_step_index": dep.get("source_step_index", -1),
                "correlation_type": dep.get("correlation_type", "response_extract"),
                "pass_to": [],
            }
        groups[key]["pass_to"].append({
            "request": dep.get("target_request"),
            "location": dep.get("target_location"),
            "step_index": dep.get("target_step_index", -1),
        })

    # Dedupe pass_to entries inside each group
    result = []
    for group in groups.values():
        seen_targets: Set[tuple] = set()
        unique_targets = []
        for target in group["pass_to"]:
            tkey = (target["request"], target["location"])
            if tkey in seen_targets:
                continue
            seen_targets.add(tkey)
            unique_targets.append(target)
        group["pass_to"] = unique_targets
        result.append(group)
    return result


def _format_pass_to_cell(
    targets: List[Dict[str, Any]],
    user_steps: List[Any],
    *,
    max_targets: int = 3,
) -> str:
    """Render pass targets as short API hints (no long CSS/URL walls)."""
    if not targets:
        return "—"
    parts = []
    shown = targets[:max_targets]
    for t in shown:
        loc = str(t.get("location") or "")
        leaf = loc.split(".")[-1] if loc else "value"
        path = _api_endpoint_label(t.get("request") or "")
        if path and path != "/":
            parts.append(f"{path} ({leaf})")
        else:
            parts.append(leaf)
    remaining = len(targets) - len(shown)
    if remaining > 0:
        parts.append(f"+{remaining} more")
    return ", ".join(parts)


def _humanize_extract(group: Dict[str, Any]) -> str:
    """One-line extract source for end users."""
    loc = str(group.get("extract_location") or "")
    req = group.get("extract_request") or ""
    ctype = group.get("correlation_type") or ""
    leaf = loc.split(".")[-1] if loc else "value"
    if ctype == "ui_extract" or loc.startswith("ui."):
        return "Page / UI after create"
    if "set-cookie" in loc.lower():
        return f"Login Set-Cookie → `{leaf}`"
    if req:
        return f"{_api_endpoint_label(req)} response → `{leaf}`"
    return f"`{leaf}`"


def _format_correlations_table(
    user_steps: List[Any],
    dependencies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
) -> str:
    """Render actionable correlations in plain language for scripting."""
    unique_deps = [
        d
        for d in _dedupe_dependencies(dependencies)
        if is_actionable_dependency(d)
        and not looks_like_person_name(str(d.get("run1_value") or ""))
    ]
    grouped = _group_dependencies_by_value(unique_deps)
    grouped = [
        g
        for g in grouped
        if not looks_like_person_name(str(g.get("run1_value") or ""))
    ]
    grouped = sorted(
        grouped,
        key=lambda g: (
            0 if (g.get("correlation_type") or "") == "response_extract" else 1,
            0 if "set-cookie" in str(g.get("extract_location") or "").lower() else 1,
            0 if str(g.get("extract_location") or "").startswith("body.$") else 1,
            -len(str(g.get("run1_value") or "")),
            str(g.get("variable") or ""),
        ),
    )
    # One row per correlation variable (prefer best source above)
    deduped_groups: List[Dict[str, Any]] = []
    seen_vars: Set[str] = set()
    for g in grouped:
        var = str(g.get("variable") or "").lower()
        if not var or var in seen_vars:
            continue
        seen_vars.add(var)
        deduped_groups.append(g)
    grouped = deduped_groups
    # Hide noisy uncorrelated leftovers from the default report — they confuse users.
    sections: List[str] = []

    if grouped:
        lines = [
            "_These are values the **server creates**. "
            "Capture them from the first place, then reuse them later "
            "(do not hardcode)._\n",
            "| Name | Example | Capture from | Reuse in |",
            "| --- | --- | --- | --- |",
        ]
        for group in grouped:
            example = group.get("run1_value") or group.get("run2_value") or ""
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_table_cell(group.get("variable")),
                        _escape_table_cell(example, max_len=36),
                        _escape_table_cell(_humanize_extract(group), max_len=60),
                        _escape_table_cell(
                            _format_pass_to_cell(group.get("pass_to", []), user_steps),
                            max_len=70,
                        ),
                    ]
                )
                + " |"
            )
        sections.append("\n".join(lines))

    if not sections:
        return (
            "_No server IDs needed beyond the session cookie jar. "
            "Script parameters + cookies first._\n"
        )

    return "\n\n".join(sections) + "\n"


def _format_transactions_table(transactions: List[Dict[str, Any]]) -> str:
    """Render transaction summaries as business/user steps (not URL dumps).

    Args:
        transactions: Transaction dictionaries with business or UI labels.

    Returns:
        Markdown table or an empty-state sentence.
    """
    if not transactions:
        return "_No transactions were identified._\n"

    lines = [
        "| Txn | Business step | User actions |",
        "| --- | --- | --- |",
    ]
    for txn in transactions:
        # Never dump http_requests / raw URLs in the chat report.
        steps = (
            txn.get("business_steps")
            or txn.get("ui_actions")
            or []
        )
        if isinstance(steps, list) and steps:
            # If legacy data still has METHOD URL lines, hide them.
            cleaned = [
                s
                for s in steps
                if not re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+https?://", str(s).strip())
            ]
            steps = cleaned or steps
            req_cell = "<br>".join(
                _escape_table_cell(s, max_len=80) for s in steps
            )
        else:
            req_cell = "—"
        if not req_cell:
            req_cell = "—"
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(txn.get("name")),
                    _escape_table_cell(txn.get("description")),
                    req_cell.replace("|", "\\|"),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def build_scripting_playbook(
    *,
    transactions: List[Dict[str, Any]],
    parameterizable_candidates: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    cookie_notes: List[Any],
) -> str:
    """Build a numbered scripting recipe for juniors.

    Args:
        transactions: Business TXN dictionaries.
        parameterizable_candidates: CSV/parameter candidates.
        dependencies: Extract-to-pass edges.
        cookie_notes: Cookie guidance notes.

    Returns:
        Markdown playbook section.
    """
    lines = ["### Scripting playbook", ""]
    step_n = 1

    # Journey frame
    txn_names = [
        t.get("name")
        for t in (transactions or [])
        if t.get("name") and t.get("name") != "Launch"
    ][:8]
    if txn_names:
        lines.append(
            f"{step_n}. **Journey phases:** " + " → ".join(f"`{n}`" for n in txn_names)
        )
        step_n += 1

    # Cookies
    cookie_names = []
    for n in cookie_notes or []:
        if isinstance(n, dict):
            name = n.get("cookie_name")
            must = n.get("must_correlate", True)
        else:
            name = getattr(n, "cookie_name", None)
            must = getattr(n, "must_correlate", True)
        if name and must:
            cookie_names.append(str(name))
    if cookie_names:
        uniq = list(dict.fromkeys(cookie_names))
        lines.append(
            f"{step_n}. **Session:** After login, keep cookie(s) "
            + ", ".join(f"`{c}`" for c in uniq)
            + " in the HTTP cookie jar for all later requests."
        )
    else:
        lines.append(
            f"{step_n}. **Session:** Enable the HTTP cookie jar after login "
            "(session cookies are usually enough)."
        )
    step_n += 1

    # CSV params
    seen_vars: Set[str] = set()
    csv_cols: List[str] = []
    for cand in parameterizable_candidates or []:
        var = str(cand.get("variable_name") or "").strip()
        if var and var not in seen_vars:
            seen_vars.add(var)
            csv_cols.append(var)
    if csv_cols:
        lines.append(
            f"{step_n}. **CSV / data file columns:** "
            + ", ".join(f"`{c}`" for c in csv_cols[:12])
            + (" …" if len(csv_cols) > 12 else "")
            + "."
        )
    else:
        lines.append(f"{step_n}. **CSV / data file:** No user parameters detected.")
    step_n += 1

    # Correlations
    actionable = [
        d
        for d in _dedupe_dependencies(dependencies or [])
        if is_actionable_dependency(d)
        and not looks_like_person_name(str(d.get("run1_value") or ""))
        and "set-cookie" not in str(d.get("source_location") or "").lower()
        and not str(d.get("target_location") or "").startswith("cookie.")
    ]
    if actionable:
        # Dedupe by variable
        seen_corr: Set[str] = set()
        for d in actionable:
            var = str(d.get("value_key") or "token")
            if var in seen_corr:
                continue
            seen_corr.add(var)
            src = _humanize_extract(
                {
                    "extract_location": d.get("source_location"),
                    "extract_request": d.get("source_request"),
                    "correlation_type": d.get("correlation_type"),
                }
            )
            lines.append(
                f"{step_n}. **Extract `{var}`** from {src}, then reuse it in later APIs "
                "(do not hardcode the recorded value)."
            )
            step_n += 1
            if step_n > 8:
                break
    else:
        lines.append(
            f"{step_n}. **Correlations:** None beyond the session cookie jar — "
            "script parameters + cookies first."
        )
        step_n += 1

    lines.append(
        f"{step_n}. **Run the draft k6** below (1 VU smoke). Wire CSV + extracts, "
        "then scale VUs."
    )
    lines.append("")
    return "\n".join(lines)


def format_correlation_report(
    user_steps: List[Any],
    run1_requests: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    parameterizable_candidates: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]] = None,
    sub_tasks: List[Dict[str, Any]] = None,
    transactions: List[Dict[str, Any]] = None,
    k6_script: str = None,
    k6_file: Dict[str, str] = None,
    include_transactions: bool = True,
    include_k6: bool = True,
    cookie_notes: List[Any] = None,
    correlation_advice_summary: str = "",
    extra_run_note: str = "",
) -> str:
    """Build the user-facing Markdown performance analysis report.

    Default product shape: playbook → params → correlations → session → TXNs → k6.
    """
    correlations = correlations or []
    transactions = transactions or []
    k6_file = k6_file or {}
    cookie_notes = cookie_notes or []

    summary_markdown = "## Performance Test Analysis\n\n"
    summary_markdown += (
        "_**Parameters** = CSV data you supply. "
        "**Correlations** = server IDs to capture and reuse. "
        "**Session** = cookie jar after login._\n\n"
    )

    # Playbook first — the product users judge
    summary_markdown += build_scripting_playbook(
        transactions=transactions,
        parameterizable_candidates=parameterizable_candidates,
        dependencies=dependencies,
        cookie_notes=cookie_notes,
    )
    summary_markdown += "\n"

    # Optional short peer summary — never LLM process chatter
    advice = (correlation_advice_summary or "").strip()
    if advice and not re.search(
        r"\b(LLM|deterministic cookie-diff|fallback|extra run)\b",
        advice,
        re.IGNORECASE,
    ):
        summary_markdown += f"_{advice}_\n\n"

    summary_markdown += "### Parameters\n\n"
    summary_markdown += _format_parameters_table(parameterizable_candidates)
    summary_markdown += "\n### Correlations\n\n"
    summary_markdown += _format_correlations_table(user_steps, dependencies, correlations)
    summary_markdown += "\n### Session (cookies)\n\n"
    try:
        from src.agents.correlation_classifier_agent import format_cookie_notes_section

        summary_markdown += format_cookie_notes_section(cookie_notes)
    except Exception:
        summary_markdown += (
            "- After login, keep the HTTP **cookie jar** enabled for session continuity.\n"
        )

    if include_transactions:
        summary_markdown += "\n### Transactions\n\n"
        summary_markdown += (
            "_Business phases — user actions only "
            "(HTTP detail is in the k6 file)._\n\n"
        )
        summary_markdown += _format_transactions_table(transactions)

    if include_k6 and (k6_script or k6_file):
        summary_markdown += "\n"
        summary_markdown += format_k6_section(
            k6_script or "",
            file_path=k6_file.get("path", ""),
            file_url=k6_file.get("file_url", ""),
            relative_path=k6_file.get("relative_path", ""),
            dependencies=dependencies,
            parameterizable_candidates=parameterizable_candidates,
        )

    return summary_markdown


def format_transactions_section(transactions: List[Dict[str, Any]]) -> str:
    """Build a headed transaction Markdown section.

    Args:
        transactions: Transaction dictionaries.

    Returns:
        Heading followed by a transaction table or empty state.
    """
    return (
        "### Transactions\n\n"
        "_Business phases for scripting — user actions only "
        "(HTTP detail stays in the k6/IR artifacts)._\n\n"
        + _format_transactions_table(transactions)
    )


def format_k6_section(
    k6_script: str,
    *,
    file_path: str = "",
    file_url: str = "",
    relative_path: str = "",
    preview_lines: int = 20,
    dependencies: List[Dict[str, Any]] = None,
    parameterizable_candidates: List[Dict[str, Any]] = None,
) -> str:
    """Render k6 artifact links, what’s wired, and a bounded preview."""
    if not k6_script and not file_path:
        return "_No k6 script available yet. Run a journey analysis first._\n"

    lines = [
        "### Load test script (k6)",
        "",
    ]

    # What’s wired
    wired: List[str] = []
    params = [
        str(c.get("variable_name"))
        for c in (parameterizable_candidates or [])
        if c.get("variable_name")
    ]
    params = list(dict.fromkeys(params))[:8]
    if params:
        wired.append("CSV params: " + ", ".join(f"`{p}`" for p in params))
    corr_vars = []
    for d in dependencies or []:
        if not is_actionable_dependency(d):
            continue
        if looks_like_person_name(str(d.get("run1_value") or "")):
            continue
        if "set-cookie" in str(d.get("source_location") or "").lower():
            continue
        v = d.get("value_key")
        if v:
            corr_vars.append(str(v))
    corr_vars = list(dict.fromkeys(corr_vars))[:6]
    if corr_vars:
        wired.append("Extracts wired: " + ", ".join(f"`{v}`" for v in corr_vars))
    wired.append("Session: cookie jar (auto)")
    lines.append("_What’s in this draft:_ " + " · ".join(wired))
    lines.append("")

    if file_path or relative_path:
        display = relative_path or file_path
        lines.append(f"- **File:** `{display}`")
        if file_url:
            lines.append(f"- **Open:** [{relative_path or file_path}]({file_url})")
        lines.append("")
    else:
        lines.append("_Script generated but not written to disk._")
        lines.append("")

    preview_src = k6_script or ""
    if preview_src:
        preview = "\n".join(preview_src.splitlines()[:preview_lines])
        truncated = len(preview_src.splitlines()) > preview_lines
        lines.append("<details>")
        lines.append("<summary>Preview (first lines)</summary>")
        lines.append("")
        lines.append("```javascript")
        lines.append(preview)
        if truncated:
            lines.append("// ... see full file ...")
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)



def build_performance_test_output(
    target_url: str,
    user_steps: List[Any],
    sub_tasks: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    parameterizable_candidates: List[Dict[str, Any]],
    transactions: List[Dict[str, Any]] = None,
    har: Dict[str, Any] = None,
    k6_script: str = None,
    load_test_ir: Dict[str, Any] = None,
    k6_file: Dict[str, str] = None,
) -> Dict[str, Any]:
    """Build structured output for downstream performance-test tooling.

    Args:
        target_url: Journey target URL.
        user_steps: Ordered journey steps.
        sub_tasks: Identified journey sub-tasks.
        correlations: Dynamic values found between runs.
        dependencies: Extract-to-pass dependency edges.
        parameterizable_candidates: User-fed parameter candidates.
        transactions: Optional transaction definitions.
        har: Optional HAR document.
        k6_script: Optional generated JavaScript.
        load_test_ir: Optional deterministic Load-Test IR.
        k6_file: Optional saved artifact metadata.

    Returns:
        Dictionary containing journey, parameterization, reconciled correlation,
        transaction, IR, and artifact sections.
    """
    unique_deps = _dedupe_dependencies(dependencies)
    grouped = _group_dependencies_by_value(unique_deps)
    uncorrelated = _uncorrelated_dynamics(correlations, unique_deps)
    transactions = transactions or []
    k6_file = k6_file or {}

    return {
        "target_url": target_url,
        "journey_steps": user_steps,
        "sub_tasks": sub_tasks,
        "parameterization": [
            {
                "variable_name": c.get("variable_name", "input_value"),
                "selector": c["selector"],
                "current_value": c["value"],
                "source": "credentials" if c.get("is_credential") else "user_input",
                "credential_key": c.get("credential_name"),
                "propagations": c.get("propagations", []),
            }
            for c in parameterizable_candidates
        ],
        "correlation": {
            "extract_pass": [
                {
                    "variable_name": g["variable"],
                    "run1_value": g.get("run1_value"),
                    "run2_value": g.get("run2_value"),
                    "extract_from": {
                        "request": g.get("extract_request"),
                        "location": g.get("extract_location"),
                        "step_index": g.get("extract_step_index"),
                    },
                    "pass_to": g.get("pass_to", []),
                    "correlation_type": g.get("correlation_type"),
                }
                for g in grouped
            ],
            "dependencies": unique_deps,
            "uncorrelated_dynamics": [
                {
                    "variable_name": c.get("dynamic_name"),
                    "location": f"{c.get('location')}.{c.get('key')}",
                    "request_url": c.get("request_url"),
                    "step_index": c.get("step_index"),
                    "run1_value": c.get("run1_value"),
                    "run2_value": c.get("run2_value"),
                }
                for c in uncorrelated
            ],
            "all_dynamic_values": correlations,
        },
        "transactions": transactions,
        "load_test_ir": load_test_ir,
        "artifacts": {
            "har": har,
            # Keep script in state for rebuilds, but UI should prefer the file path
            "k6_script": k6_script,
            "k6_file": k6_file,
            "load_test_ir": load_test_ir,
        },
    }
