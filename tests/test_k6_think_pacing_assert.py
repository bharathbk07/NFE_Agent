"""Tests for think time, pacing, and per-TXN content assertions."""

from __future__ import annotations

from src.utils.k6_generator import emit_k6_from_ir
from src.utils.k6_healer import heal_load_test_ir
from src.utils.load_test_ir import (
    apply_txn_anchor_assertions,
    derive_request_assertion,
    normalize_think_time_s,
    select_anchor_request_index,
)


def test_normalize_think_time_scalar_and_range():
    assert normalize_think_time_s(1) == {"min": 1.0, "max": 1.0}
    assert normalize_think_time_s({"min": 2, "max": 5}) == {"min": 2.0, "max": 5.0}
    assert normalize_think_time_s({"min": 4, "max": 2}) == {"min": 4.0, "max": 4.0}
    assert normalize_think_time_s(None) == {"min": 1.0, "max": 3.0}


def test_select_anchor_prefers_correlation_then_post():
    reqs = [
        {"method": "GET", "url": "https://example.com/page", "resource_type": "document"},
        {"method": "GET", "url": "https://example.com/api/token", "resource_type": "xhr"},
        {"method": "POST", "url": "https://example.com/api/create", "resource_type": "xhr"},
    ]
    corrs = [
        {
            "var": "csrf",
            "extract": {
                "from_request": "https://example.com/api/token",
                "from_location": "body.$.token",
            },
        }
    ]
    assert select_anchor_request_index(reqs, corrs) == 1
    assert select_anchor_request_index(reqs, []) == 2


def test_select_anchor_skips_soft_and_static():
    reqs = [
        {
            "method": "GET",
            "url": "https://example.com/app.css",
            "resource_type": "stylesheet",
        },
        {
            "method": "GET",
            "url": "https://example.com/api/data",
            "resource_type": "xhr",
            "soft_check": True,
        },
        {
            "method": "GET",
            "url": "https://example.com/api/main",
            "resource_type": "xhr",
        },
    ]
    assert select_anchor_request_index(reqs, []) == 2


def test_derive_assertion_json_stable_keys():
    assertion = derive_request_assertion(
        {"method": "POST", "url": "https://example.com/api/create", "status": 201},
        captured={
            "status": 201,
            "response_body": '{"success":true,"data":{"id":99},"token":"abc"}',
        },
    )
    assert assertion["expect_status"] == [201]
    assert "$.success" in assertion.get("json_path_exists", [])
    assert "$.data" in assertion.get("json_path_exists", [])
    # Must not assert on dynamic token values
    body = str(assertion)
    assert "abc" not in body or "token" not in assertion.get("body_contains", [])


def test_derive_assertion_json_from_correlation_path():
    assertion = derive_request_assertion(
        {"method": "POST", "url": "https://example.com/api/employees/1/requests"},
        captured={
            "status": 200,
            "response_body": '{"data":{"id":47}}',
        },
        correlations=[
            {
                "var": "requestId",
                "extract": {
                    "from_request": "https://example.com/api/employees/1/requests",
                    "from_location": "body.$.data.id",
                },
            }
        ],
    )
    assert "$.data.id" in assertion.get("json_path_exists", [])


def test_derive_assertion_html_title():
    assertion = derive_request_assertion(
        {"method": "GET", "url": "https://example.com/dashboard", "status": 200},
        captured={
            "status": 200,
            "response_body": (
                "<!doctype html><html><head><title>OrangeHRM</title></head>"
                "<body><h1>Dashboard</h1></body></html>"
            ),
        },
    )
    assert assertion["expect_status"] == [200, 201]
    assert any("OrangeHRM" in c for c in assertion.get("body_contains", []))


def test_emit_think_time_random_and_pacing():
    ir = {
        "version": 1,
        "target_url": "https://example.com/",
        "workload": {"pacing_s": 60, "think_time_s": {"min": 2, "max": 4}},
        "vars": [],
        "correlations": [],
        "transactions": [
            {
                "name": "Launch",
                "think_time_s": 0,
                "requests": [
                    {
                        "method": "GET",
                        "url": "https://example.com/",
                        "headers": {},
                        "body": None,
                        "body_type": "empty",
                        "assertion_anchor": True,
                        "assertion": {
                            "type": "status_and_body",
                            "expect_status": [200],
                        },
                    }
                ],
            }
        ],
    }
    script = emit_k6_from_ir(ir)
    assert "USER CONFIG" in script
    assert "const CONFIG" in script
    assert '"min": 2' in script and '"max": 4' in script
    assert '"pacing_s": 60' in script
    assert "CONFIG.thinkTime" in script
    assert "CONFIG.pacing_s" in script
    assert "thresholds: CONFIG.thresholds" in script
    assert "CONFIG.workload" in script
    assert "NFE_THINK_TIME" in script
    assert "Math.random()" in script
    assert "__nfeIterStart" in script
    assert "assertion:" in script
    assert "expect_status" in script
    # USER CONFIG / vars appear before first TXN function
    assert script.index("const CONFIG") < script.index("export function Launch")
    assert script.index("const vars") < script.index("export function Launch")
    assert script.index("export const options") < script.index("export function Launch")


def test_emit_scalar_think_time_compat():
    ir = {
        "version": 1,
        "target_url": "https://example.com/",
        "vars": [],
        "correlations": [],
        "transactions": [
            {
                "name": "Launch",
                "think_time_s": 1,
                "requests": [
                    {
                        "method": "GET",
                        "url": "https://example.com/",
                        "headers": {},
                        "body": None,
                        "body_type": "empty",
                    }
                ],
            }
        ],
    }
    script = emit_k6_from_ir(ir)
    assert '"min": 1' in script and '"max": 1' in script
    assert "CONFIG.thinkTime.min" in script


def test_apply_anchor_assertions_marks_one_request():
    txns = [
        {
            "name": "Create",
            "mode": "protocol",
            "requests": [
                {
                    "method": "GET",
                    "url": "https://example.com/form",
                    "resource_type": "document",
                    "status": 200,
                },
                {
                    "method": "POST",
                    "url": "https://example.com/api/create",
                    "resource_type": "xhr",
                    "status": 200,
                },
            ],
        }
    ]
    apply_txn_anchor_assertions(
        txns,
        [],
        network_requests=[
            {
                "method": "POST",
                "url": "https://example.com/api/create",
                "status": 200,
                "response_body": '{"ok":true,"data":{}}',
            }
        ],
    )
    reqs = txns[0]["requests"]
    anchors = [r for r in reqs if r.get("assertion_anchor")]
    assert len(anchors) == 1
    assert anchors[0]["method"] == "POST"
    assert anchors[0].get("assertion", {}).get("json_path_exists")


def test_healer_never_softens_anchor():
    ir = {
        "version": 1,
        "target_url": "https://example.com/",
        "vars": [],
        "correlations": [],
        "transactions": [
            {
                "name": "Browse",
                "mode": "protocol",
                "requests": [
                    {
                        "method": "GET",
                        "url": "https://example.com/chrome/menu",
                        "resource_type": "xhr",
                    },
                    {
                        "method": "GET",
                        "url": "https://example.com/api/important",
                        "resource_type": "xhr",
                        "assertion_anchor": True,
                        "assertion": {
                            "type": "status_and_body",
                            "expect_status": [200],
                            "json_path_exists": ["$.ok"],
                        },
                        "synthesized_assertion": True,
                    },
                ],
            }
        ],
    }
    healed, notes = heal_load_test_ir(
        ir,
        {
            "ok": False,
            "failed_checks": ["Browse GET expect status", "Browse GET json path $.ok"],
            "failed_urls": [],
            "stdout": "",
            "stderr": "",
        },
        attempt=1,
    )
    assert any("Content assertion failed" in n for n in notes)
    for txn in healed["transactions"]:
        for r in txn.get("requests") or []:
            if r.get("assertion_anchor") or r.get("assertion"):
                assert not r.get("soft_check")


def _proto_txn(name: str, *, with_assert: bool = True) -> dict:
    req = {
        "method": "POST",
        "url": f"https://example.com/api/{name}",
        "resource_type": "xhr",
        "status": 200,
    }
    if with_assert:
        req["assertion_anchor"] = True
        req["assertion"] = {
            "type": "status_and_body",
            "expect_status": [200],
            "json_path_exists": ["$.ok"],
        }
    return {"name": name, "mode": "protocol", "requests": [req]}


def test_validate_txn_assertions_requires_one_per_protocol_txn():
    from src.utils.load_test_ir import validate_txn_assertions

    ir = {
        "transactions": [
            _proto_txn("A"),
            _proto_txn("B"),
            _proto_txn("C", with_assert=False),
            {"name": "login", "mode": "browser", "requests": []},
        ]
    }
    ok, notes = validate_txn_assertions(ir)
    assert ok is False
    assert any("C" in n for n in notes)
    assert any("2/3" in n or "coverage" in n.lower() for n in notes)

    ir2 = {
        "transactions": [
            _proto_txn("A"),
            _proto_txn("B"),
            _proto_txn("C"),
            {"name": "login", "mode": "browser", "requests": []},
        ]
    }
    ok2, notes2 = validate_txn_assertions(ir2)
    assert ok2 is True
    assert notes2 == []


def test_prepare_gate_fixes_then_blocks_if_still_short():
    from src.utils.k6_assertion_gate import (
        assertion_coverage_failure_result,
        prepare_ir_and_script_for_smoke,
    )
    from src.utils.load_test_ir import validate_txn_assertions

    ir = {
        "version": 1,
        "target_url": "https://example.com/",
        "vars": [],
        "correlations": [],
        "transactions": [
            _proto_txn("A", with_assert=False),
            _proto_txn("B", with_assert=False),
            _proto_txn("C", with_assert=False),
        ],
    }
    script = emit_k6_from_ir(ir)
    # Without captures, ensure still derives status-only assertions from request.status
    ir2, script2, ok, _notes = prepare_ir_and_script_for_smoke(ir, script)
    assert ok is True
    assert script2.count("assertion:") >= 3
    ok_v, _ = validate_txn_assertions(ir2, script=script2)
    assert ok_v is True

    # Empty protocol TXN cannot get an assertion → gate fails
    bad = {
        "version": 1,
        "target_url": "https://example.com/",
        "vars": [],
        "correlations": [],
        "transactions": [
            {"name": "Empty", "mode": "protocol", "requests": []},
            _proto_txn("A"),
        ],
    }
    s = emit_k6_from_ir(bad)
    _, _, ok_bad, notes_bad = prepare_ir_and_script_for_smoke(bad, s)
    assert ok_bad is False
    fail = assertion_coverage_failure_result(notes_bad)
    assert fail["ok"] is False
    assert fail.get("assertion_gate_failed") is True
    assert "assertion coverage failed" in fail["summary"]
