"""Unit tests for HTTP payload data-randomization middleware."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from src.utils.data_randomization import (
    DataRandomizationMiddleware,
    STRATEGY_EMAIL,
    STRATEGY_NUMERIC,
    apply_randomization_to_ir,
    choose_strategy,
    classify_non_randomizable_url,
    filter_randomized_correlations,
    filter_randomized_dependencies,
    generate_random_value,
    is_auth_endpoint,
    is_deliberate_randomization,
    is_protected_field,
    is_randomizable_field,
)


def test_field_classification():
    assert is_randomizable_field("payload.user.email")
    assert is_randomizable_field("email")
    assert is_randomizable_field("orderId")
    assert is_randomizable_field("query.username")
    assert not is_randomizable_field("password")
    assert not is_randomizable_field("payload._token")
    assert is_protected_field("csrf_token")
    assert is_protected_field("payload.password")


def test_auth_and_payment_classification():
    assert is_auth_endpoint("https://app.example.com/auth/login")
    assert is_auth_endpoint("https://app.example.com/oauth/token")
    assert not is_auth_endpoint("https://app.example.com/api/orders")
    assert classify_non_randomizable_url(
        "https://api.stripe.com/v1/charges", "POST"
    )
    assert classify_non_randomizable_url(
        "https://shop.example.com/checkout/pay", "POST"
    )
    assert classify_non_randomizable_url("https://app.example.com/api/users", "POST") is None


def test_generate_random_value_strategies():
    email = generate_random_value("user@test.com", STRATEGY_EMAIL)
    assert email.endswith("@test.com")
    assert email != "user@test.com"
    assert "_nfe_" in email

    num = generate_random_value("99482", STRATEGY_NUMERIC)
    assert num.isdigit()
    assert num != "99482"

    assert choose_strategy("payload.user.email", "a@b.com") == STRATEGY_EMAIL
    assert choose_strategy("orderId", "99482") == STRATEGY_NUMERIC


def test_harvest_json_and_form_and_query():
    mw = DataRandomizationMiddleware()
    count = mw.harvest_from_requests(
        [
            {
                "method": "POST",
                "url": "https://app.example.com/api/users",
                "headers": {"content-type": "application/json"},
                "post_data": {
                    "user": {"email": "user@test.com", "username": "testuser"},
                    "orderId": "99482",
                    "password": "secret",
                    "_token": "csrf-abc",
                },
                "body_type": "json",
            },
            {
                "method": "POST",
                "url": "https://app.example.com/forms/submit",
                "headers": {"content-type": "application/x-www-form-urlencoded"},
                "post_data": "email=form@test.com&password=x",
                "body_type": "form",
            },
            {
                "method": "GET",
                "url": "https://app.example.com/search?email=q@test.com&t=123",
                "headers": {},
                "post_data": None,
            },
            {
                "method": "POST",
                "url": "https://app.example.com/auth/login",
                "headers": {"content-type": "application/json"},
                "post_data": {"username": "admin", "password": "pw"},
                "body_type": "json",
            },
            {
                "method": "POST",
                "url": "https://api.stripe.com/v1/charges",
                "headers": {"content-type": "application/json"},
                "post_data": {"amount": 100, "email": "pay@test.com"},
                "body_type": "json",
            },
        ]
    )
    assert count >= 4  # email, username, orderId, form email, query email
    paths = {t.field_path for t in mw.transforms}
    assert "payload.user.email" in paths
    assert "payload.user.username" in paths
    assert "payload.orderId" in paths
    assert "payload.email" in paths or any("email" in p for p in paths)
    # Auth login and password / csrf must not be harvested
    assert not any(t.field_path.endswith("password") for t in mw.transforms)
    assert not any("_token" in t.field_path for t in mw.transforms)
    assert not any("/auth/login" in (t.request_url or "") for t in mw.transforms)

    non_rand = mw.non_randomizable_routes()
    assert any("stripe" in (r.get("url") or "") for r in non_rand)

    ledger = mw.ledger_entries()
    assert ledger
    assert all(e.get("run1_value") and e.get("run2_value") for e in ledger)
    assert all(e["run1_value"] != e["run2_value"] for e in ledger)


def test_rewrite_json_body_and_query():
    mw = DataRandomizationMiddleware()
    mw.harvest_from_requests(
        [
            {
                "method": "POST",
                "url": "https://app.example.com/api/users?email=q@test.com",
                "headers": {"content-type": "application/json"},
                "post_data": {"email": "user@test.com", "orderId": "99482"},
                "body_type": "json",
            }
        ]
    )
    body_t = next(t for t in mw.transforms if t.field_path == "payload.email")
    order_t = next(t for t in mw.transforms if t.field_path == "payload.orderId")
    query_t = next(t for t in mw.transforms if t.field_path == "query.email")

    rewritten = mw.rewrite_request(
        method="POST",
        url="https://app.example.com/api/users?email=q@test.com",
        headers={"content-type": "application/json"},
        post_data=json.dumps({"email": "user@test.com", "orderId": "99482"}),
    )
    assert rewritten["modified"] is True
    parsed_body = json.loads(rewritten["post_data"])
    assert parsed_body["email"] == body_t.run2_value
    assert parsed_body["orderId"] == order_t.run2_value
    q = parse_qs(urlparse(rewritten["url"]).query)
    assert q["email"][0] == query_t.run2_value


def test_ledger_filters_correlations_not_csrf():
    ledger = [
        {
            "run1_value": "user@test.com",
            "run2_value": "user_nfe_1_abc@test.com",
            "field_path": "payload.email",
            "location": "body",
        }
    ]
    corrs = [
        {
            "location": "body",
            "key": "email",
            "run1_value": "user@test.com",
            "run2_value": "user_nfe_1_abc@test.com",
        },
        {
            "location": "header",
            "key": "x-csrf-token",
            "run1_value": "tok-a",
            "run2_value": "tok-b",
        },
    ]
    filtered = filter_randomized_correlations(corrs, ledger)
    assert len(filtered) == 1
    assert filtered[0]["key"] == "x-csrf-token"
    assert is_deliberate_randomization(
        run1_value="user@test.com",
        run2_value="user_nfe_1_abc@test.com",
        field_path="payload.email",
        ledger=ledger,
    )


def test_filter_dependencies():
    ledger = [
        {
            "run1_value": "testuser",
            "run2_value": "testuser_nfe_1_x",
            "field_path": "payload.username",
        }
    ]
    deps = [
        {
            "run1_value": "testuser",
            "run2_value": "testuser_nfe_1_x",
            "value_key": "username",
            "target_location": "body.username",
            "correlation_type": "response_extract",
        },
        {
            "run1_value": "sess-1",
            "run2_value": "sess-2",
            "value_key": "session_id",
            "target_location": "cookie.session",
            "correlation_type": "response_extract",
        },
    ]
    kept = filter_randomized_dependencies(deps, ledger)
    assert len(kept) == 1
    assert kept[0]["value_key"] == "session_id"


def test_apply_randomization_to_ir_flags():
    ir = {
        "vars": [
            {"name": "email", "value": "user@test.com"},
            {"name": "static_note", "value": "hello"},
        ],
        "transactions": [
            {
                "name": "CreateUser",
                "requests": [
                    {
                        "method": "POST",
                        "url": "https://app.example.com/api/users",
                        "body": {"email": "${email}"},
                    },
                    {
                        "method": "POST",
                        "url": "https://api.stripe.com/v1/charges",
                        "body": {"amount": 1},
                    },
                ],
            }
        ],
    }
    ledger = [
        {
            "run1_value": "user@test.com",
            "run2_value": "user_nfe_1@test.com",
            "field_path": "payload.email",
            "method": "POST",
            "url_path": "/api/users",
            "strategy": STRATEGY_EMAIL,
        }
    ]
    non_rand = [
        {
            "method": "POST",
            "url": "https://api.stripe.com/v1/charges",
            "url_path": "/v1/charges",
            "reason": "third_party_payment_host:stripe.com",
            "handling": "mock_response",
        }
    ]
    out = apply_randomization_to_ir(ir, ledger=ledger, non_randomizable=non_rand)
    assert out["vars"][0]["randomize"] is True
    assert out["vars"][0]["randomize_strategy"] == STRATEGY_EMAIL
    assert not out["vars"][1].get("randomize")

    reqs = out["transactions"][0]["requests"]
    assert "payload.email" in (reqs[0].get("randomized_fields") or [])
    assert reqs[1].get("requires_manual_data") is True
    assert reqs[1].get("mock_in_load_test") is True
    assert reqs[1].get("ir_flag") == "manual_test_data_or_mock"


def test_roundtrip_to_dict_from_dict():
    mw = DataRandomizationMiddleware()
    mw.harvest_from_requests(
        [
            {
                "method": "POST",
                "url": "https://app.example.com/api/users",
                "headers": {"content-type": "application/json"},
                "post_data": {"email": "a@b.com"},
                "body_type": "json",
            }
        ]
    )
    restored = DataRandomizationMiddleware.from_dict(mw.to_dict())
    assert len(restored.transforms) == len(mw.transforms)
    assert restored.ledger_entries()
    assert restored.transforms[0].run2_value == mw.transforms[0].run2_value


def test_should_mock_stripe():
    mw = DataRandomizationMiddleware()
    mw.harvest_from_requests(
        [
            {
                "method": "POST",
                "url": "https://api.stripe.com/v1/charges",
                "headers": {},
                "post_data": {"amount": 1},
            }
        ]
    )
    mock = mw.should_mock("POST", "https://api.stripe.com/v1/charges")
    assert mock is not None
    assert "stripe" in mock.reason
