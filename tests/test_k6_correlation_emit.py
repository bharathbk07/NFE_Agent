"""Tests for k6 correlation extract path emission (gjson)."""

from __future__ import annotations

from src.utils.k6_generator import (
    _body_location_to_k6_json_path,
    _extract_snippets_for_txn,
    emit_k6_from_ir,
)


def test_body_location_to_k6_json_path_array_index():
    assert (
        _body_location_to_k6_json_path("body.$.data[0].empNumber")
        == "data.0.empNumber"
    )
    assert _body_location_to_k6_json_path("body.$.data.id") == "data.id"
    assert (
        _body_location_to_k6_json_path("body.data[0][1].x") == "data.0.1.x"
    )


def test_extract_snippet_uses_gjson_array_path():
    corrs = [
        {
            "var": "empnumber",
            "extract": {
                "from_request": "https://example.com/api/v2/pim/employees?nameOrId=e",
                "from_location": "body.$.data[0].empNumber",
            },
        }
    ]
    lines = _extract_snippets_for_txn(
        "Assign_claim",
        corrs,
        ["https://example.com/api/v2/pim/employees?nameOrId=e&includeEmployees=onlyCurrent"],
        res_var="res2",
    )
    joined = "\n".join(lines)
    assert 'res2.json("data.0.empNumber")' in joined
    assert 'res2.json("data[0].empNumber")' not in joined
    assert "__nfeExt !== undefined" in joined


def test_emit_seeds_correlation_vars_and_gjson_path():
    ir = {
        "version": 1,
        "target_url": "https://example.com/login",
        "vars": [],
        "correlations": [
            {
                "var": "empnumber",
                "extract": {
                    "from_request": "https://example.com/api/employees?q=e",
                    "from_location": "body.$.data[0].empNumber",
                },
                "pass": {
                    "to_request": "https://example.com/api/employees/95/requests",
                    "to_location": "path.employees",
                },
                "run1_value": "95",
                "run2_value": "95",
            },
            {
                "var": "requestId",
                "extract": {
                    "from_request": "https://example.com/api/employees/95/requests",
                    "from_location": "body.$.data.id",
                },
                "pass": {
                    "to_request": "https://example.com/api/requests/47",
                    "to_location": "path.requests",
                },
                "run1_value": "47",
            },
        ],
        "transactions": [
            {
                "name": "Assign_claim",
                "think_time_s": 0,
                "requests": [
                    {
                        "method": "GET",
                        "url": "https://example.com/api/employees?q=e",
                        "headers": {"Accept": "application/json"},
                        "body": None,
                        "expected_statuses": [200],
                    },
                    {
                        "method": "POST",
                        "url": "https://example.com/api/employees/${empnumber}/requests",
                        "headers": {"Content-Type": "application/json"},
                        "body": {"remarks": "x"},
                        "expected_statuses": [200],
                    },
                ],
            }
        ],
    }
    script = emit_k6_from_ir(ir)
    assert 'empnumber: "95"' in script
    assert 'requestId: "47"' in script
    assert 'json("data.0.empNumber")' in script
    assert 'json("data[0].empNumber")' not in script
    # Default smoke must not abortOnFail (lets heal see full run)
    assert "abortOnFail" not in script
