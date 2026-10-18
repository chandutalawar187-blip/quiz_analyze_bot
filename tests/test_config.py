"""Environment-backed configuration tests."""

import os
import unittest
from unittest.mock import patch

from quizcord_bot.config import Settings


class SettingsTests(unittest.TestCase):
    def test_requires_token_and_guild_id(self) -> None:
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "DISCORD_BOT_TOKEN"):
                Settings.from_environment()

        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, {"DISCORD_BOT_TOKEN": "test-token"}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "BOT_GUILD_ID"):
                Settings.from_environment()

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

    def test_gmail_smtp_password_reset_settings_must_be_configured_together(self) -> None:
        values = {
            "DISCORD_BOT_TOKEN": "test-token",
            "BOT_GUILD_ID": "67890",
            "GMAIL_SMTP_USERNAME": "quizcord@gmail.com",
        }
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "GMAIL_SMTP_USERNAME"):
                Settings.from_environment()

        values["GMAIL_SMTP_APP_PASSWORD"] = "abcd efgh ijkl mnop"
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            settings = Settings.from_environment()
        self.assertEqual(
            settings.gmail_smtp_username,
            "quizcord@gmail.com",
        )
        self.assertEqual(settings.gmail_smtp_app_password, "abcdefghijklmnop")

        values["GMAIL_SMTP_USERNAME"] = "quizcord@gmail.com\r\nBcc: user@gmail.com"
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ, values, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "cannot contain whitespace"):
                Settings.from_environment()

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
