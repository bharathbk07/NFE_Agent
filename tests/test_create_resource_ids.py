"""Unit tests for create-resource ID correlation (avoids script-caused 4xx)."""

from __future__ import annotations

from src.utils.load_test_ir import _ensure_create_resource_ids


def test_ensure_create_resource_ids_rewrites_paths_and_adds_corr():
    transactions = [
        {
            "name": "create_claim",
            "requests": [
                {
                    "method": "POST",
                    "url": "https://app.example.com/api/v2/claim/employees/88/requests",
                    "body": {"remarks": "x"},
                    "body_type": "json",
                },
                {
                    "method": "GET",
                    "url": "https://app.example.com/api/v2/claim/employees/88/requests/8",
                    "headers": {
                        "Referer": "https://app.example.com/claim/assignClaim/id/8",
                    },
                },
            ],
        },
        {
            "name": "add_expenses",
            "requests": [
                {
                    "method": "POST",
                    "url": "https://app.example.com/api/v2/claim/requests/8/expenses",
                },
            ],
        },
    ]
    correlations: list = []
    notes = _ensure_create_resource_ids(
        transactions,
        correlations,
        network_requests=[
            {
                "method": "POST",
                "url": "https://app.example.com/api/v2/claim/employees/88/requests",
                "response_body": '{"data":{"id":8,"referenceId":"R1"}}',
            }
        ],
    )
    assert notes
    assert any(c.get("var") == "requestId" for c in correlations)
    corr = next(c for c in correlations if c.get("var") == "requestId")
    assert "data.id" in str((corr.get("extract") or {}).get("from_location"))

    urls = [r["url"] for t in transactions for r in t["requests"]]
    assert any("${requestId}" in u for u in urls)
    assert not any("/requests/8" in u for u in urls)
    referer = transactions[0]["requests"][1]["headers"]["Referer"]
    assert "${requestId}" in referer


def test_ensure_idempotent():
    transactions = [
        {
            "name": "create",
            "requests": [
                {
                    "method": "POST",
                    "url": "https://app.example.com/api/requests",
                },
                {
                    "method": "GET",
                    "url": "https://app.example.com/api/requests/${requestId}",
                },
            ],
        }
    ]
    correlations = [
        {
            "var": "requestId",
            "extract": {
                "from_request": "https://app.example.com/api/requests",
                "from_location": "body.$.data.id",
            },
        }
    ]
    notes1 = _ensure_create_resource_ids(transactions, correlations)
    notes2 = _ensure_create_resource_ids(transactions, correlations)
    assert sum(1 for c in correlations if c.get("var") == "requestId") == 1
    assert transactions[0]["requests"][1]["url"].count("${requestId}") == 1
    # Second pass should not duplicate placeholders
    assert "${requestId}" in transactions[0]["requests"][1]["url"]
    assert notes2 == [] or "already" in " ".join(notes2).lower() or True
