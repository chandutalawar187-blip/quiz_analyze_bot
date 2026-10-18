"""Sentry telemetry setup and privacy-filter tests."""

import os
import unittest
from unittest.mock import patch

from quizcord_bot.config import Settings
from quizcord_bot.telemetry import (
    _scrub_error_event,
    _scrub_transaction,
    initialize_sentry,
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
        self.assertFalse(options["send_default_pii"])
        self.assertEqual(
            [integration.transaction_style for integration in options["integrations"]],
            ["endpoint", "endpoint"],
        )

    def test_error_and_performance_events_remove_request_and_span_data(self) -> None:
        error_event = {
            "request": {"url": "/faculty/students/4GM24CS018", "data": "password"},
            "breadcrumbs": {"values": [{"message": "sensitive"}]},
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


if __name__ == "__main__":
    unittest.main()
