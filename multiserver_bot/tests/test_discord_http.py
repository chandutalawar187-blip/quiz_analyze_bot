"""Tests for coordinated Discord API requests."""

import asyncio
import time
import unittest
from unittest.mock import AsyncMock

import httpx

from quizcord_bot.discord_http import (
    DiscordRateLimitError,
    DiscordRequestCoordinator,
    retry_after_seconds,
)


class DiscordRequestCoordinatorTests(unittest.TestCase):
    def test_retries_after_discord_retry_after(self) -> None:
        async def run() -> None:
            coordinator = DiscordRequestCoordinator()
            responses = iter(
                [
                    httpx.Response(
                        429,
                        json={"retry_after": 0.06, "global": True},
                    ),
                    httpx.Response(200),
                ]
            )
            attempts = 0

            async def request(_url: str) -> httpx.Response:
                nonlocal attempts
                attempts += 1
                return next(responses)

            async def trace_request(
                _operation: str, request_fn, *args, **kwargs
            ) -> httpx.Response:
                return await request_fn(*args, **kwargs)

            started_at = time.monotonic()
            response = await coordinator.request(
                "discord.oauth.current_user",
                trace_request,
                request,
                "https://discord.com/api/v10/users/@me",
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(attempts, 2)
            self.assertGreaterEqual(time.monotonic() - started_at, 0.06)

        asyncio.run(run())

    def test_serializes_concurrent_calls(self) -> None:
        async def run() -> None:
            coordinator = DiscordRequestCoordinator()
            active_requests = 0
            max_active_requests = 0

            async def request(_url: str) -> httpx.Response:
                nonlocal active_requests, max_active_requests
                active_requests += 1
                max_active_requests = max(max_active_requests, active_requests)
                await asyncio.sleep(0.01)
                active_requests -= 1
                return httpx.Response(200)

            async def trace_request(
                _operation: str, request_fn, *args, **kwargs
            ) -> httpx.Response:
                return await request_fn(*args, **kwargs)

            await asyncio.gather(
                *(
                    coordinator.request(
                        "discord.oauth.current_user",
                        trace_request,
                        request,
                        "https://discord.com/api/v10/users/@me",
                    )
                    for _ in range(3)
                )
            )

            self.assertEqual(max_active_requests, 1)

        asyncio.run(run())

    def test_does_not_retry_rate_limit_without_retry_after(self) -> None:
        async def run() -> None:
            coordinator = DiscordRequestCoordinator()
            request = AsyncMock(return_value=httpx.Response(429, text="Access denied"))

            async def trace_request(
                _operation: str, request_fn, *args, **kwargs
            ) -> httpx.Response:
                return await request_fn(*args, **kwargs)

            with self.assertRaises(DiscordRateLimitError) as error:
                await coordinator.request(
                    "discord.oauth.token_exchange",
                    trace_request,
                    request,
                    "https://discord.com/api/v10/oauth2/token",
                )

            self.assertEqual(error.exception.retry_after, 1.0)
            request.assert_awaited_once()

        asyncio.run(run())

    def test_reads_json_retry_after_and_never_uses_html_body(self) -> None:
        json_response = httpx.Response(
            429,
            json={"retry_after": 1.25, "global": True},
        )
        cloudflare_response = httpx.Response(
            429,
            text="<html>Access denied</html>",
        )

        self.assertEqual(retry_after_seconds(json_response), 1.25)
        self.assertIsNone(retry_after_seconds(cloudflare_response))


if __name__ == "__main__":
    unittest.main()
