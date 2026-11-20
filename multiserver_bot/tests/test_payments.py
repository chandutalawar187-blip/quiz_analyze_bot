"""Razorpay request and signature tests."""

import hashlib
import hmac
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from quizcord_bot.payments import (
    SUBSCRIPTION_AMOUNT_PAISE,
    RazorpayAPIError,
    RazorpayClient,
    verify_webhook_signature,
)


def _mock_http_client(response: httpx.Response) -> tuple[Mock, Mock]:
    http_client = Mock()
    http_client.request = AsyncMock(return_value=response)
    context = Mock()
    context.__aenter__ = AsyncMock(return_value=http_client)
    context.__aexit__ = AsyncMock(return_value=False)
    return context, http_client


class RazorpaySignatureTests(unittest.TestCase):
    def test_webhook_signature_uses_exact_raw_body(self) -> None:
        body = b'{"event":"subscription.charged"}'
        secret = "webhook-secret"
        signature = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()

        self.assertTrue(verify_webhook_signature(body, signature, secret))
        self.assertFalse(
            verify_webhook_signature(body + b" ", signature, secret)
        )
        self.assertFalse(
            verify_webhook_signature(body, "invalid-signature", secret)
        )


class RazorpayClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_creation_uses_monthly_inr_price(self) -> None:
        response = httpx.Response(
            200,
            json={"id": "plan_test"},
            request=httpx.Request("POST", "https://api.razorpay.com/v1/plans"),
        )
        context, http_client = _mock_http_client(response)
        client = RazorpayClient("rzp_test_id", "test-secret")

        with patch(
            "quizcord_bot.payments.httpx.AsyncClient", return_value=context
        ) as async_client:
            result = await client.create_plan()

        self.assertEqual(result["id"], "plan_test")
        async_client.assert_called_once_with(
            auth=("rzp_test_id", "test-secret"), timeout=15
        )
        self.assertEqual(
            http_client.request.await_args.args[:2],
            ("POST", "https://api.razorpay.com/v1/plans"),
        )
        plan = http_client.request.await_args.kwargs["json"]
        self.assertEqual(plan["period"], "monthly")
        self.assertEqual(plan["item"]["amount"], SUBSCRIPTION_AMOUNT_PAISE)
        self.assertEqual(plan["item"]["currency"], "INR")

    async def test_cancellation_is_scheduled_for_the_period_end(self) -> None:
        response = httpx.Response(
            200,
            json={"id": "sub_test"},
            request=httpx.Request(
                "POST",
                "https://api.razorpay.com/v1/subscriptions/sub_test/cancel",
            ),
        )
        context, http_client = _mock_http_client(response)
        client = RazorpayClient("rzp_test_id", "test-secret")

        with patch(
            "quizcord_bot.payments.httpx.AsyncClient", return_value=context
        ):
            await client.cancel_subscription("sub_test")

        self.assertEqual(
            http_client.request.await_args.kwargs["json"],
            {"cancel_at_cycle_end": True},
        )

    async def test_provider_http_errors_are_reported(self) -> None:
        response = httpx.Response(
            503,
            request=httpx.Request(
                "GET",
                "https://api.razorpay.com/v1/subscriptions/sub_test",
            ),
        )
        context, _ = _mock_http_client(response)
        client = RazorpayClient("rzp_test_id", "test-secret")

        with (
            patch(
                "quizcord_bot.payments.httpx.AsyncClient",
                return_value=context,
            ),
            self.assertRaises(RazorpayAPIError) as error,
        ):
            await client.fetch_subscription("sub_test")

        self.assertEqual(error.exception.status_code, 503)
