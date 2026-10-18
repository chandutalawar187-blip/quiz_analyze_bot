"""Optional Sentry error reporting and performance tracing."""

import logging
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.logging import LoggingIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from quizcord_bot.config import Settings


_SAFE_LOG_ATTRIBUTES = {
    "code.function.name",
    "code.line.number",
    "logger.name",
    "sentry.origin",
}
_SAFE_METRIC_ATTRIBUTES = {"http.method", "http.route", "http.status_code"}


def initialize_sentry(settings: Settings) -> bool:
    if settings.sentry_dsn is None:
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profile_session_sample_rate=settings.sentry_profile_sample_rate,
        profile_lifecycle="trace",
        send_default_pii=False,
        enable_logs=True,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
            LoggingIntegration(
                level=None,
                event_level=logging.ERROR,
                sentry_logs_level=logging.WARNING,
            ),
        ],
        before_send=_scrub_error_event,
        before_send_transaction=_scrub_transaction,
        before_send_log=_scrub_log,
        before_send_metric=_scrub_metric,
    )
    return True


def record_http_request(
    method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    attributes = {
        "http.method": method,
        "http.route": route,
        "http.status_code": status_code,
    }
    sentry_sdk.metrics.count("http.server.request", 1, attributes=attributes)
    sentry_sdk.metrics.distribution(
        "http.server.request.duration",
        duration_seconds * 1000,
        unit="millisecond",
        attributes=attributes,
    )


def _scrub_error_event(
    event: dict[str, Any], _hint: dict[str, Any]
) -> dict[str, Any]:
    event.pop("request", None)
    event.pop("breadcrumbs", None)
    event.pop("logentry", None)
    return event


def _scrub_log(log: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    severity = log.get("severity_text", "unknown")
    log["body"] = f"Application log ({severity})"
    attributes = log.get("attributes")
    if isinstance(attributes, dict):
        log["attributes"] = {
            key: value
            for key, value in attributes.items()
            if key in _SAFE_LOG_ATTRIBUTES
        }
    return log


def _scrub_metric(
    metric: dict[str, Any], _hint: dict[str, Any]
) -> dict[str, Any]:
    attributes = metric.get("attributes")
    if isinstance(attributes, dict):
        metric["attributes"] = {
            key: value
            for key, value in attributes.items()
            if key in _SAFE_METRIC_ATTRIBUTES
        }
    return metric


def _scrub_transaction(
    event: dict[str, Any], _hint: dict[str, Any]
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
