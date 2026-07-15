from typing import List, Dict, Any, Set


def get_step_label(idx: int, user_steps: List[Any]) -> str:
    """Returns a human-readable step label for step_index."""
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
    """Sanitize a value for use in a markdown table cell."""
    text = str(value or "").replace("|", "\\|").replace("\n", " ").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _uncorrelated_dynamics(
    correlations: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Return dynamic values that differ between runs but have no extract→pass link."""
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


def _format_parameters_table(parameterizable_candidates: List[Dict[str, Any]]) -> str:
    seen_params: Set[tuple] = set()
    unique_parameterizable = []
    for cand in parameterizable_candidates:
        cand_key = (cand["selector"], cand["value"])
        if cand_key not in seen_params:
            seen_params.add(cand_key)
            unique_parameterizable.append(cand)

    if not unique_parameterizable:
        return "_No parameterizable inputs detected._\n"

    lines = [
        "| Variable | Selector | Value | Load Test | Network Propagation |",
        "| --- | --- | --- | --- | --- |",
    ]
    for cand in unique_parameterizable:
        var_name = cand.get("variable_name", "input_value")
        if cand.get("is_credential"):
            load_test = f"credential: {cand.get('credential_name', var_name)}"
        else:
            load_test = f"${{{var_name}}}"
        propagations = cand.get("propagations") or []
        propagation_text = "; ".join(propagations) if propagations else "Client-side only"
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table_cell(var_name),
                    _escape_table_cell(cand["selector"]),
                    _escape_table_cell(cand["value"]),
                    _escape_table_cell(load_test),
                    _escape_table_cell(propagation_text),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _short_url(url: str, max_len: int = 70) -> str:
    """Compact a URL for table display."""
    text = str(url or "")
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _group_dependencies_by_value(
    dependencies: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Collapse extract→pass edges into one row per variable value:
    extract-from (source) + all pass-to targets.
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


def _format_pass_to_cell(targets: List[Dict[str, Any]], user_steps: List[Any]) -> str:
    if not targets:
        return "—"
    parts = []
    for t in targets:
        step = get_step_label(t.get("step_index", -1), user_steps)
        parts.append(
            f"{step} → `{t.get('location')}` in `{_short_url(t.get('request'))}`"
        )
    return "<br>".join(parts)


def _format_correlations_table(
    user_steps: List[Any],
    dependencies: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]],
) -> str:
    unique_deps = _dedupe_dependencies(dependencies)
    grouped = _group_dependencies_by_value(unique_deps)
    uncorrelated = _uncorrelated_dynamics(correlations, unique_deps)
    sections: List[str] = []

    if grouped:
        lines = [
            "| Variable | Run 1 | Run 2 | Extract From (request) | Pass To (request) |",
            "| --- | --- | --- | --- | --- |",
        ]
        for group in grouped:
            extract_step = get_step_label(group.get("extract_step_index", -1), user_steps)
            extract_cell = (
                f"{extract_step}<br>"
                f"`{group.get('extract_location')}` in "
                f"`{_short_url(group.get('extract_request'))}`"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_table_cell(group.get("variable")),
                        _escape_table_cell(group.get("run1_value"), max_len=40),
                        _escape_table_cell(group.get("run2_value"), max_len=40),
                        extract_cell.replace("|", "\\|"),
                        _format_pass_to_cell(group.get("pass_to", []), user_steps).replace(
                            "|", "\\|"
                        ),
                    ]
                )
                + " |"
            )
        sections.append("\n".join(lines))

    # Uncorrelated leftovers: values that appear only once and have no extract→pass link
    if uncorrelated:
        # Dedupe by (variable, run1, run2) so the same value isn't listed per URL
        seen_vals: Set[tuple] = set()
        leftover_lines = [
            "| Variable | Step | Location | Request | Run 1 | Run 2 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        leftover_count = 0
        for corr in uncorrelated:
            val_key = (
                corr.get("dynamic_name"),
                corr.get("run1_value"),
                corr.get("run2_value"),
            )
            if val_key in seen_vals:
                continue
            seen_vals.add(val_key)
            leftover_count += 1
            leftover_lines.append(
                "| "
                + " | ".join(
                    [
                        _escape_table_cell(corr.get("dynamic_name")),
                        _escape_table_cell(
                            get_step_label(corr.get("step_index", -1), user_steps)
                        ),
                        _escape_table_cell(f"{corr.get('location')}.{corr.get('key')}"),
                        _escape_table_cell(_short_url(corr.get("request_url"))),
                        _escape_table_cell(corr.get("run1_value"), max_len=40),
                        _escape_table_cell(corr.get("run2_value"), max_len=40),
                    ]
                )
                + " |"
            )
        if leftover_count:
            sections.append(
                "**Uncorrelated (no extract→pass link)**\n\n" + "\n".join(leftover_lines)
            )

    if not sections:
        return "_No correlation values found between Run 1 and Run 2._\n"

    return "\n\n".join(sections) + "\n"


def _format_transactions_table(transactions: List[Dict[str, Any]]) -> str:
    if not transactions:
        return "_No transactions were identified._\n"

    lines = [
        "| Txn | Description | Requests / Actions |",
        "| --- | --- | --- |",
    ]
    for txn in transactions:
        # Prefer protocol HTTP labels; fall back to mixed request_urls / UI
        requests = (
            txn.get("http_requests")
            or txn.get("request_urls")
            or txn.get("ui_actions")
            or []
        )
        if isinstance(requests, list):
            req_cell = "<br>".join(f"`{_escape_table_cell(r, max_len=100)}`" for r in requests)
        else:
            req_cell = _escape_table_cell(requests)
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


def format_correlation_report(
    user_steps: List[Any],
    run1_requests: List[Dict[str, Any]],
    dependencies: List[Dict[str, Any]],
    parameterizable_candidates: List[Dict[str, Any]],
    correlations: List[Dict[str, Any]] = None,
    sub_tasks: List[Dict[str, Any]] = None,
    transactions: List[Dict[str, Any]] = None,
    k6_script: str = None,
    include_transactions: bool = False,
    include_k6: bool = False,
) -> str:
    """
    Default report: Parameters + Correlations only.
    Transactions / k6 are included only when explicitly requested.
    """
    correlations = correlations or []
    transactions = transactions or []

    summary_markdown = "## Performance Test Analysis\n\n"
    summary_markdown += "### 1. Parameters\n\n"
    summary_markdown += _format_parameters_table(parameterizable_candidates)
    summary_markdown += "\n### 2. Correlations\n\n"
    summary_markdown += _format_correlations_table(user_steps, dependencies, correlations)

    if include_transactions:
        summary_markdown += "\n### 3. Transactions\n\n"
        summary_markdown += _format_transactions_table(transactions)

    if include_k6 and k6_script:
        section_num = 4 if include_transactions else 3
        summary_markdown += f"\n### {section_num}. Load Test Stub (k6)\n\n"
        summary_markdown += (
            "_Starter script from params / correlations / TXNs. "
            "Complete extract→pass logic before running under load._\n\n"
        )
        summary_markdown += "```javascript\n"
        clipped = k6_script if len(k6_script) <= 6000 else k6_script[:6000] + "\n// ... truncated ...\n"
        summary_markdown += clipped
        summary_markdown += "\n```\n"

    if not include_transactions and not include_k6:
        summary_markdown += (
            "\n_Ask for **transactions** or **k6 script** if you want those sections._\n"
        )

    return summary_markdown


def format_transactions_section(transactions: List[Dict[str, Any]]) -> str:
    return "### Transactions\n\n" + _format_transactions_table(transactions)


def format_k6_section(k6_script: str) -> str:
    if not k6_script:
        return "_No k6 script available yet. Run a journey analysis first._\n"
    clipped = k6_script if len(k6_script) <= 8000 else k6_script[:8000] + "\n// ... truncated ...\n"
    return (
        "### Load Test Stub (k6)\n\n"
        "_Starter script — complete extract→pass logic before load._\n\n"
        f"```javascript\n{clipped}\n```\n"
    )


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
) -> Dict[str, Any]:
    """Build structured JSON output for performance test tooling."""
    unique_deps = _dedupe_dependencies(dependencies)
    grouped = _group_dependencies_by_value(unique_deps)
    uncorrelated = _uncorrelated_dynamics(correlations, unique_deps)
    transactions = transactions or []

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
        "artifacts": {
            "har": har,
            "k6_script": k6_script,
        },
    }
