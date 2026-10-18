"""Environment-backed configuration for the bot."""

import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    token: str
    quizcord_bot_id: int | None
    database_path: Path
    guild_id: int | None
    dashboard_public_url: str | None
    discord_oauth_client_id: str | None
    discord_oauth_client_secret: str | None
    faculty_role_id: int | None
    dashboard_session_secret: str | None
    turnstile_site_key: str | None
    turnstile_secret_key: str | None
    dashboard_host: str
    dashboard_port: int
    sentry_dsn: str | None
    sentry_traces_sample_rate: float
    sentry_profile_sample_rate: float

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv(PROJECT_ROOT / ".env")

        token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("Set DISCORD_BOT_TOKEN in the project .env file.")

        quizcord_bot_id = _optional_discord_id("QUIZCORD_BOT_ID")
        guild_id = _optional_discord_id("BOT_GUILD_ID")
        database_value = os.getenv("BOT_DATABASE_PATH", "server_results.db").strip()
        if not database_value:
            raise ValueError("BOT_DATABASE_PATH cannot be empty.")
        database_path = Path(database_value).expanduser()
        if not database_path.is_absolute():
            database_path = PROJECT_ROOT / database_path
        dashboard_public_url = os.getenv("DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
        oauth_client_id = os.getenv("DISCORD_OAUTH_CLIENT_ID", "").strip()
        oauth_client_secret = os.getenv("DISCORD_OAUTH_CLIENT_SECRET", "").strip()
        faculty_role_id = _optional_discord_id("FACULTY_ROLE_ID")
        session_secret = os.getenv("DASHBOARD_SESSION_SECRET", "").strip()
        turnstile_site_key = os.getenv("TURNSTILE_SITE_KEY", "").strip()
        turnstile_secret_key = os.getenv("TURNSTILE_SECRET_KEY", "").strip()
        if bool(turnstile_site_key) != bool(turnstile_secret_key):
            raise ValueError(
                "Set TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY together."
            )
        dashboard_settings = (
            dashboard_public_url,
            oauth_client_id,
            oauth_client_secret,
            session_secret,
        )
        if any(dashboard_settings) and not all(dashboard_settings):
            raise ValueError(
                "Set DASHBOARD_PUBLIC_URL, DISCORD_OAUTH_CLIENT_ID, "
                "DISCORD_OAUTH_CLIENT_SECRET, and DASHBOARD_SESSION_SECRET together."
            )
        if dashboard_public_url:
            parsed_url = urlsplit(dashboard_public_url)
            if (
                parsed_url.scheme != "https"
                or not parsed_url.netloc
                or parsed_url.path
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError(
                    "DASHBOARD_PUBLIC_URL must be an HTTPS origin without a path."
                )
            _parse_discord_id("DISCORD_OAUTH_CLIENT_ID", oauth_client_id)
            if len(session_secret) < 32:
                raise ValueError(
                    "DASHBOARD_SESSION_SECRET must contain at least 32 characters."
                )
        dashboard_port = _parse_port(
            os.getenv("PORT", os.getenv("DASHBOARD_PORT", "8765")).strip()
        )
        dashboard_host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip()
        if not dashboard_host:
            raise ValueError("DASHBOARD_HOST cannot be empty.")
        sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
        sentry_traces_sample_rate = _parse_sample_rate(
            "SENTRY_TRACES_SAMPLE_RATE",
            os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1").strip(),
        )
        sentry_profile_sample_rate = _parse_sample_rate(
            "SENTRY_PROFILE_SAMPLE_RATE",
            os.getenv("SENTRY_PROFILE_SAMPLE_RATE", "0.01").strip(),
        )

        return cls(
            token=token,
            quizcord_bot_id=quizcord_bot_id,
            database_path=database_path,
            guild_id=guild_id,
            dashboard_public_url=dashboard_public_url or None,
            discord_oauth_client_id=oauth_client_id or None,
            discord_oauth_client_secret=oauth_client_secret or None,
            faculty_role_id=faculty_role_id,
            dashboard_session_secret=session_secret or None,
            turnstile_site_key=turnstile_site_key or None,
            turnstile_secret_key=turnstile_secret_key or None,
            dashboard_host=dashboard_host,
            dashboard_port=dashboard_port,
            sentry_dsn=sentry_dsn or None,
            sentry_traces_sample_rate=sentry_traces_sample_rate,
            sentry_profile_sample_rate=sentry_profile_sample_rate,
        )


def _required_discord_id(name: str) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} in the project .env file.")
    return _parse_discord_id(name, value)


def _optional_discord_id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    return _parse_discord_id(name, value) if value else None


def _parse_discord_id(name: str, value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive numeric Discord ID.") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive numeric Discord ID.")
    return parsed


def _parse_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise ValueError("DASHBOARD_PORT must be a valid TCP port.") from error
    if not 1 <= port <= 65535:
        raise ValueError("DASHBOARD_PORT must be between 1 and 65535.")
    return port


def _parse_sample_rate(name: str, value: str) -> float:
    try:
        rate = float(value)
    except ValueError as error:
        raise ValueError(
            f"{name} must be a number between 0 and 1."
        ) from error
    if not math.isfinite(rate) or not 0 <= rate <= 1:
        raise ValueError(
            f"{name} must be a number between 0 and 1."
        )
    return rate
