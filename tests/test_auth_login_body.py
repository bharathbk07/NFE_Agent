"""Login form must use credential vars — never ***REDACTED*** literals."""

from __future__ import annotations

from src.utils.k6_generator import emit_k6_from_ir
from src.utils.load_test_ir import _ensure_auth_csrf


def test_ensure_auth_replaces_redacted_password():
    transactions = [
        {
            "name": "Login",
            "mode": "protocol",
            "requests": [
                {
                    "method": "POST",
                    "url": "https://example.com/web/index.php/auth/validate",
                    "body_type": "form",
                    "body": {
                        "_token": "stale-csrf",
                        "username": "Admin",
                        "password": "***REDACTED***",
                    },
                }
            ],
        }
    ]
    correlations: list = []
    vars_list = [
        {"name": "username", "value": "Admin", "is_credential": True},
        {"name": "password", "value": "admin123", "is_credential": True},
    ]
    _ensure_auth_csrf(
        transactions,
        correlations,
        origin="https://example.com",
        vars_list=vars_list,
    )
    body = transactions[0]["requests"][0]["body"]
    assert body["_token"] == "${csrf_token}"
    assert body["username"] == "${username}"
    assert body["password"] == "${password}"
    assert "***REDACTED***" not in str(body)


def test_emit_login_uses_vars_password():
    ir = {
        "version": 1,
        "target_url": "https://example.com/web/index.php/auth/login",
        "origin": "https://example.com",
        "vars": [
            {"name": "username", "value": "Admin", "is_credential": True},
            {"name": "password", "value": "admin123", "is_credential": True},
        ],
        "correlations": [],
        "transactions": [
            {
                "name": "Login",
                "mode": "protocol",
                "think_time_s": 0,
                "requests": [
                    {
                        "method": "POST",
                        "url": "https://example.com/web/index.php/auth/validate",
                        "headers": {
                            "content-type": "application/x-www-form-urlencoded"
                        },
                        "body_type": "form",
                        "body": {
                            "_token": "${csrf_token}",
                            "username": "${username}",
                            "password": "***REDACTED***",
                        },
                        "expected_statuses": [200, 302],
                    }
                ],
            }
        ],
    }
    script = emit_k6_from_ir(ir)
    assert "vars.password" in script
    assert '"password": "***REDACTED***"' not in script
    assert '"password": "***REDACTED***"' not in script.replace(" ", "")
