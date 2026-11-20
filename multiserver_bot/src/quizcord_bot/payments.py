"""Razorpay subscription API and signature helpers."""

import hashlib
import hmac
import logging
from typing import Any

import httpx


LOGGER = logging.getLogger("quizcord_analytics.payments")
RAZORPAY_API_BASE = "https://api.razorpay.com/v1"
SERVER_AMOUNT_PAISE = 56_300
DEVELOPER_ALLOCATION_PAISE = 10_000
SUBSCRIPTION_AMOUNT_PAISE = SERVER_AMOUNT_PAISE + DEVELOPER_ALLOCATION_PAISE
SUBSCRIPTION_CURRENCY = "INR"
SUBSCRIPTION_TOTAL_COUNT = 120
SUBSCRIPTION_PLAN_KEY = "server-dashboard-inr-663-monthly-v1"


class RazorpayAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RazorpayClient:
    def __init__(self, key_id: str, key_secret: str) -> None:
        self.key_id = key_id
        self._key_secret = key_secret

    async def create_plan(self) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/plans",
            {
                "period": "monthly",
                "interval": 1,
                "item": {
                    "name": "Quizcord Analytics server subscription",
                    "amount": SUBSCRIPTION_AMOUNT_PAISE,
                    "currency": SUBSCRIPTION_CURRENCY,
                    "description": (
                        "Monthly Quizcord Analytics dashboard subscription"
                    ),
                },
                "notes": {"plan_key": SUBSCRIPTION_PLAN_KEY},
            },
        )

    async def create_subscription(
        self, plan_id: str, guild_id: int
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/subscriptions",
            {
                "plan_id": plan_id,
                "total_count": SUBSCRIPTION_TOTAL_COUNT,
                "quantity": 1,
                "customer_notify": True,
                "notes": {
                    "product": "quizcord_analytics",
                    "guild_id": str(guild_id),
                },
            },
        )

    async def fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"/subscriptions/{subscription_id}"
        )

    async def cancel_subscription(
        self, subscription_id: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/subscriptions/{subscription_id}/cancel",
            {"cancel_at_cycle_end": True},
        )

    async def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                auth=(self.key_id, self._key_secret), timeout=15
            ) as client:
                response = await client.request(
                    method,
                    f"{RAZORPAY_API_BASE}{path}",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as error:
            LOGGER.warning(
                "Razorpay API returned HTTP %d for %s %s.",
                error.response.status_code,
                method,
                path,
            )
            raise RazorpayAPIError(
                "Razorpay could not complete the request.",
                error.response.status_code,
            ) from None
        except httpx.RequestError:
            LOGGER.exception("Razorpay API request failed for %s %s.", method, path)
            raise RazorpayAPIError("Could not connect to Razorpay.") from None
        except ValueError:
            LOGGER.exception("Razorpay returned invalid JSON for %s %s.", method, path)
            raise RazorpayAPIError("Razorpay returned an invalid response.") from None
        if not isinstance(data, dict):
            raise RazorpayAPIError("Razorpay returned an invalid response.")
        return data


def verify_webhook_signature(
    payload: bytes, received_signature: str, webhook_secret: str
) -> bool:
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_signature, received_signature)
