"""Sentry telemetry setup and privacy-filter tests."""

import os
import unittest
from unittest.mock import patch

from quizcord_bot.config import Settings
from quizcord_bot.telemetry import (
    _scrub_error_event,
    _scrub_log,
    _scrub_metric,
    _scrub_transaction,
    initialize_sentry,
    record_http_request,
)


class SentryTelemetryTests(unittest.TestCase):
    def _settings(self) -> Settings:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "SENTRY_DSN": "https://public@example.ingest.sentry.io/123",
                "SENTRY_TRACES_SAMPLE_RATE": "0.1",
            },
            clear=True,
        ):
            return Settings.from_environment()

    def test_sentry_is_not_initialized_without_a_dsn(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {"DISCORD_BOT_TOKEN": "test-token"}, clear=True
        ):
            settings = Settings.from_environment()

        with patch("quizcord_bot.telemetry.sentry_sdk.init") as init:
            self.assertFalse(initialize_sentry(settings))

        init.assert_not_called()

    def test_sentry_initialization_enables_tracing_without_default_pii(self) -> None:
        settings = self._settings()

        with patch("quizcord_bot.telemetry.sentry_sdk.init") as init:
            self.assertTrue(initialize_sentry(settings))

        options = init.call_args.kwargs
        self.assertEqual(options["dsn"], settings.sentry_dsn)
        self.assertEqual(options["traces_sample_rate"], 0.1)
        self.assertEqual(options["profile_session_sample_rate"], 0.01)
        self.assertEqual(options["profile_lifecycle"], "trace")
        self.assertTrue(options["enable_logs"])
        self.assertFalse(options["send_default_pii"])
        self.assertEqual(
            [
                integration.transaction_style
                for integration in options["integrations"][:2]
            ],
            ["endpoint", "endpoint"],
        )
        self.assertEqual(
            options["integrations"][2].__class__.__name__,
            "LoggingIntegration",
        )

    def test_error_and_performance_events_remove_request_and_span_data(self) -> None:
        error_event = {
            "request": {"url": "/faculty/students/4GM24CS018", "data": "password"},
            "breadcrumbs": {"values": [{"message": "sensitive"}]},
            "logentry": {"message": "Sensitive password value"},
            "exception": {"values": [{"type": "ValueError"}]},
        }
        self.assertEqual(
            _scrub_error_event(error_event, {}),
            {"exception": {"values": [{"type": "ValueError"}]}},
        )

        transaction = {
            "request": {"url": "/faculty/students/4GM24CS018"},
            "spans": [
                {
                    "op": "http.client",
                    "description": "GET /guilds/67890/members/12345",
                    "data": {"url": "https://discord.com/private"},
                }
            ],
        }
        scrubbed = _scrub_transaction(transaction, {})
        self.assertNotIn("request", scrubbed)
        self.assertEqual(
            scrubbed["spans"],
            [{"op": "http.client", "description": "http.client"}],
        )

    def test_log_scrubbing_removes_message_and_unapproved_attributes(self) -> None:
        log = {
            "severity_text": "warning",
            "body": "Sensitive password value",
            "attributes": {
                "logger.name": "quizcord_analytics.dashboard",
                "code.function.name": "verify_faculty_password",
                "sentry.message.template": "Password was %s",
                "sentry.message.parameter.0": "secret",
                "user.id": "12345",
            },
        }

        scrubbed = _scrub_log(log, {})

        self.assertEqual(scrubbed["body"], "Application log (warning)")
        self.assertEqual(
            scrubbed["attributes"],
            {
                "logger.name": "quizcord_analytics.dashboard",
                "code.function.name": "verify_faculty_password",
            },
        )

    def test_metric_scrubbing_and_http_metric_names_are_low_cardinality(self) -> None:
        metric = {
            "name": "http.server.request",
            "attributes": {
                "http.method": "GET",
                "http.route": "/faculty/students/{usn}",
                "http.status_code": 200,
                "student_usn": "4GM24CS018",
                "user.id": "12345",
            },
        }
        scrubbed = _scrub_metric(metric, {})
        self.assertEqual(
            scrubbed["attributes"],
            {
                "http.method": "GET",
                "http.route": "/faculty/students/{usn}",
                "http.status_code": 200,
            },
        )

        with (
            patch("quizcord_bot.telemetry.sentry_sdk.metrics.count") as count,
            patch(
                "quizcord_bot.telemetry.sentry_sdk.metrics.distribution"
            ) as distribution,
        ):
            record_http_request(
                "GET", "/faculty/students/{usn}", 200, 0.125
            )

        count.assert_called_once_with(
            "http.server.request",
            1,
            attributes={
                "http.method": "GET",
                "http.route": "/faculty/students/{usn}",
                "http.status_code": 200,
            },
        )
        distribution.assert_called_once_with(
            "http.server.request.duration",
            125,
            unit="millisecond",
            attributes={
                "http.method": "GET",
                "http.route": "/faculty/students/{usn}",
                "http.status_code": 200,
            },
        )


if __name__ == "__main__":
    unittest.main()
