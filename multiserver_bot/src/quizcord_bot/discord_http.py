"""Coordinated Discord API requests for the dashboard."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar


_Response = TypeVar("_Response")
_MAX_RATE_LIMIT_RETRIES = 2
_MIN_REQUEST_INTERVAL_SECONDS = 0.05
_MAX_FALLBACK_RETRY_SECONDS = 60.0
LOGGER = logging.getLogger("quizcord_analytics.discord_http")


class DiscordRateLimitError(Exception):
    """A Discord 429 response with no upstream response body exposed."""

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        if retry_after is None:
            message = "Discord is temporarily rate-limiting requests."
        else:
            message = (
                "Discord is temporarily rate-limiting requests "
                f"for {retry_after:g} seconds."
            )
        super().__init__(message)


class DiscordRequestCoordinator:
    """Serializes dashboard Discord requests and honors 429 retry delays."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._last_request_at = 0.0
        self._fallback_retry_seconds = 1.0

    async def request(
        self,
        operation: str,
        trace_request: Callable[..., Awaitable[_Response]],
        request: Callable[..., Awaitable[_Response]],
        *args: Any,
        **kwargs: Any,
    ) -> _Response:
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            async with self._lock:
                while True:
                    now = time.monotonic()
                    wait_seconds = max(
                        self._blocked_until - now,
                        self._last_request_at
                        + _MIN_REQUEST_INTERVAL_SECONDS
                        - now,
                    )
                    if wait_seconds <= 0:
                        break
                    await asyncio.sleep(wait_seconds)

                response = await trace_request(
                    operation, request, *args, **kwargs
                )
                self._last_request_at = time.monotonic()
                if getattr(response, "status_code", None) != 429:
                    self._fallback_retry_seconds = 1.0
                    return response

                retry_after = retry_after_seconds(response)
                if retry_after is None:
                    retry_after = self._fallback_retry_seconds
                    self._fallback_retry_seconds = min(
                        self._fallback_retry_seconds * 2,
                        _MAX_FALLBACK_RETRY_SECONDS,
                    )
                    self._blocked_until = max(
                        self._blocked_until,
                        time.monotonic() + retry_after,
                    )
                    LOGGER.warning(
                        "Discord returned HTTP 429 for %s without a retry delay; "
                        "using a %.1f-second local cooldown.",
                        operation,
                        retry_after,
                    )
                    raise DiscordRateLimitError(retry_after)

                self._fallback_retry_seconds = 1.0
                self._blocked_until = max(
                    self._blocked_until,
                    time.monotonic() + retry_after,
                )
                if attempt >= _MAX_RATE_LIMIT_RETRIES:
                    LOGGER.warning(
                        "Discord kept rate-limiting %s after %d attempts.",
                        operation,
                        attempt + 1,
                    )
                    raise DiscordRateLimitError(retry_after)
                LOGGER.info(
                    "Discord rate-limited %s; retrying after %.2f seconds "
                    "(attempt %d of %d).",
                    operation,
                    retry_after,
                    attempt + 1,
                    _MAX_RATE_LIMIT_RETRIES + 1,
                )

        raise RuntimeError("Discord request retry loop exited unexpectedly.")


def retry_after_seconds(response: Any) -> float | None:
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        body = None
    if isinstance(body, dict):
        delay = _valid_delay(body.get("retry_after"))
        if delay is not None:
            return delay

    headers = getattr(response, "headers", {})
    for header in ("Retry-After", "X-RateLimit-Reset-After"):
        value = headers.get(header)
        delay = _valid_delay(value)
        if delay is not None:
            return delay
        if header == "Retry-After" and isinstance(value, str):
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    return None


def _valid_delay(value: Any) -> float | None:
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay
