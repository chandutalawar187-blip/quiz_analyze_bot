"""Environment-backed configuration tests."""

import os
import unittest
from unittest.mock import patch

from quizcord_bot.config import Settings


class SettingsTests(unittest.TestCase):
    def test_requires_token_but_not_a_single_server_id(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "DISCORD_BOT_TOKEN"):
                Settings.from_environment()

        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {"DISCORD_BOT_TOKEN": "test-token"}, clear=True
        ):
            settings = Settings.from_environment()
        self.assertIsNone(settings.guild_id)

    def test_reads_ids_and_database_path_from_environment(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "QUIZCORD_BOT_ID": "12345",
                "BOT_GUILD_ID": "67890",
                "BOT_DATABASE_PATH": "test-results.db",
                "FACULTY_ROLE_ID": "54321",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.token, "test-token")
        self.assertEqual(settings.quizcord_bot_id, 12345)
        self.assertEqual(settings.guild_id, 67890)
        self.assertEqual(settings.database_path.name, "test-results.db")
        self.assertEqual(settings.faculty_role_id, 54321)

    def test_quizcord_bot_id_can_be_omitted_for_automatic_detection(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "BOT_GUILD_ID": "67890",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertIsNone(settings.quizcord_bot_id)

    def test_sentry_is_optional_and_uses_a_conservative_trace_sample_rate(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {"DISCORD_BOT_TOKEN": "test-token"}, clear=True
        ):
            settings = Settings.from_environment()

        self.assertIsNone(settings.sentry_dsn)
        self.assertEqual(settings.sentry_traces_sample_rate, 0.1)

        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "SENTRY_DSN": "https://public@example.ingest.sentry.io/123",
                "SENTRY_TRACES_SAMPLE_RATE": "0.25",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(
            settings.sentry_dsn,
            "https://public@example.ingest.sentry.io/123",
        )
        self.assertEqual(settings.sentry_traces_sample_rate, 0.25)

    def test_sentry_trace_sample_rate_must_be_between_zero_and_one(self) -> None:
        for value in ("not-a-number", "-0.1", "1.1", "nan", "inf"):
            with self.subTest(value=value):
                with patch("quizcord_bot.config.load_dotenv"), patch.dict(
                    os.environ,
                    {
                        "DISCORD_BOT_TOKEN": "test-token",
                        "SENTRY_TRACES_SAMPLE_RATE": value,
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "SENTRY_TRACES_SAMPLE_RATE"
                    ):
                        Settings.from_environment()

    def test_dashboard_is_optional_without_oauth_configuration(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "BOT_GUILD_ID": "67890",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertIsNone(settings.dashboard_public_url)
        self.assertEqual(settings.dashboard_host, "127.0.0.1")
        self.assertEqual(settings.dashboard_port, 8765)

    def test_dashboard_configuration_requires_all_oauth_values(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "BOT_GUILD_ID": "67890",
                "DASHBOARD_PUBLIC_URL": "https://analytics.example.com",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "Set DASHBOARD_PUBLIC_URL"):
                Settings.from_environment()

    def test_dashboard_requires_https_and_a_strong_session_secret(self) -> None:
        values = {
            "DISCORD_BOT_TOKEN": "test-token",
            "BOT_GUILD_ID": "67890",
            "DASHBOARD_PUBLIC_URL": "http://analytics.example.com",
            "DISCORD_OAUTH_CLIENT_ID": "12345",
            "DISCORD_OAUTH_CLIENT_SECRET": "oauth-secret",
            "DASHBOARD_SESSION_SECRET": "s" * 40,
        }
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "must be an HTTPS origin"):
                Settings.from_environment()

        values["DASHBOARD_PUBLIC_URL"] = "https://analytics.example.com"
        values["DASHBOARD_SESSION_SECRET"] = "too-short"
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "at least 32 characters"):
                Settings.from_environment()

    def test_turnstile_keys_must_be_configured_together(self) -> None:
        values = {
            "DISCORD_BOT_TOKEN": "test-token",
            "BOT_GUILD_ID": "67890",
            "TURNSTILE_SITE_KEY": "turnstile-site-key",
        }
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "TURNSTILE_SITE_KEY"):
                Settings.from_environment()

        values["TURNSTILE_SECRET_KEY"] = "turnstile-secret-key"
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.turnstile_site_key, "turnstile-site-key")
        self.assertEqual(settings.turnstile_secret_key, "turnstile-secret-key")

    def test_railway_port_overrides_local_dashboard_port(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "BOT_GUILD_ID": "67890",
                "PORT": "31876",
                "DASHBOARD_PORT": "8765",
            },
            clear=True,
        ):
            settings = Settings.from_environment()

        self.assertEqual(settings.dashboard_port, 31876)


if __name__ == "__main__":
    unittest.main()
