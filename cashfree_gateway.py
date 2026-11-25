"""Small server-side Cashfree PG client; credentials never leave this module."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class CashfreeError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def mode() -> str:
    return os.getenv("CASHFREE_ENV", "sandbox").strip().lower()


def is_configured() -> bool:
    return bool(client_id() and client_secret())


def is_simulation_mode() -> bool:
    """Returns True if local sandbox simulation is explicitly requested or using mock keys."""
    return mode() == "sandbox" and (
        os.getenv("CASHFREE_SIMULATION", "0") == "1"
        or client_id().startswith("TEST_GW_")
    )


def client_id() -> str:
    return os.getenv("CASHFREE_CLIENT_ID") or os.getenv("CASHFREE_APP_ID", "")


def client_secret() -> str:
    return os.getenv("CASHFREE_CLIENT_SECRET") or os.getenv("CASHFREE_SECRET_KEY", "")


def webhook_secret() -> str:
    return os.getenv("CASHFREE_WEBHOOK_SECRET") or client_secret()


def _base_url() -> str:
    environment = mode()
    if environment == "sandbox":
        return "https://sandbox.cashfree.com/pg"
    if environment == "production":
        return "https://api.cashfree.com/pg"
    raise CashfreeError("CASHFREE_ENV must be sandbox or production.", 503)


def _request(method: str, path: str, body: dict[str, Any] | None = None, idempotency_key: str = "") -> Any:
    if not is_configured():
        raise CashfreeError("Cashfree credentials are not configured on the server.", 503)

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-api-version": os.getenv("CASHFREE_API_VERSION", "2023-08-01"),
        "x-client-id": client_id(),
        "x-client-secret": client_secret(),
    }
    if idempotency_key:
        headers["x-idempotency-key"] = idempotency_key

    data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
    request = Request(_base_url() + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            response_body = response.read(2 * 1024 * 1024)
    except HTTPError as error:
        error.close()
        raise CashfreeError("Cashfree rejected the payment request.", 502) from None
    except (TimeoutError, URLError, OSError) as error:
        raise CashfreeError("Cashfree could not be reached. Please retry.", 503) from error

    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CashfreeError("Cashfree returned an invalid response.", 502) from error


def create_order(
    order_id: str,
    amount: float,
    customer_id: str,
    customer_email: str,
    customer_phone: str,
    return_url: str,
) -> dict[str, Any]:
    if is_simulation_mode():
        return {
            "order_id": order_id,
            "order_amount": amount,
            "order_currency": "INR",
            "order_status": "ACTIVE",
            "payment_session_id": f"session_sandbox_sim_{order_id}",
        }

    result = _request(
        "POST",
        "/orders",
        {
            "order_id": order_id,
            "order_amount": amount,
            "order_currency": "INR",
            "customer_details": {
                "customer_id": customer_id,
                "customer_email": customer_email,
                "customer_phone": customer_phone,
            },
            "order_meta": {"return_url": return_url},
            "order_note": "GW Smart Analytics dashboard and report",
        },
        idempotency_key=order_id,
    )
    if not isinstance(result, dict):
        raise CashfreeError("Cashfree returned an invalid order response.", 502)
    return result


def fetch_order(order_id: str) -> dict[str, Any]:
    if is_simulation_mode():
        return {
            "order_id": order_id,
            "order_amount": 10.0,
            "order_currency": "INR",
            "order_status": "PAID",
        }

    result = _request("GET", f"/orders/{quote(order_id, safe='')}")
    if not isinstance(result, dict):
        raise CashfreeError("Cashfree returned an invalid order response.", 502)
    return result


def fetch_order_payments(order_id: str) -> list[dict[str, Any]]:
    if is_simulation_mode():
        return [
            {
                "order_id": order_id,
                "payment_status": "SUCCESS",
                "payment_currency": "INR",
                "payment_amount": 10.0,
                "cf_payment_id": f"cf_sim_{order_id[-12:]}",
                "payment_method": "upi",
            }
        ]

    result = _request("GET", f"/orders/{quote(order_id, safe='')}/payments")
    if not isinstance(result, list):
        raise CashfreeError("Cashfree returned an invalid payment response.", 502)
    return [payment for payment in result if isinstance(payment, dict)]


def verify_webhook_signature(timestamp: str, signature: str, raw_body: bytes) -> bool:
    secret = webhook_secret()
    if not secret or not timestamp or not signature:
        return False
    expected = base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + raw_body,
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return hmac.compare_digest(expected, signature)
