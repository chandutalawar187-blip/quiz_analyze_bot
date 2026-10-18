"""Optional Sentry error reporting and performance tracing."""

from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from quizcord_bot.config import Settings


def initialize_sentry(settings: Settings) -> bool:
    if settings.sentry_dsn is None:
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        before_send=_scrub_error_event,
        before_send_transaction=_scrub_transaction,
    )
    return True


def _scrub_error_event(
    event: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any]:
    event.pop("request", None)
    event.pop("breadcrumbs", None)
    return event


def _scrub_transaction(
    event: dict[str, Any], hint: dict[str, Any]
) -> dict[str, Any]:
    event.pop("request", None)
    for span in event.get("spans", []):
        if not isinstance(span, dict):
            continue
        span.pop("data", None)
        operation = span.get("op")
        if isinstance(operation, str):
            span["description"] = operation
    return event
