"""Regression tests for quiz dashboard and member notification behavior."""

from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quizcord_bot.cogs.analytics import AnalyticsCog


class QuizNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_leaderboard_does_not_fetch_members_for_usn_dms(self) -> None:
        database = Mock()
        database.get_quiz_session_for_message.return_value = SimpleNamespace(
            session_id="session"
        )
        database.record_quiz_leaderboard.return_value = SimpleNamespace()
        guild = SimpleNamespace(id=1, fetch_members=AsyncMock())
        message = SimpleNamespace(
            guild=guild,
            channel=SimpleNamespace(id=2),
            id=3,
            author=Mock(),
            webhook_id=None,
            edited_at=None,
            created_at=datetime.now(timezone.utc),
        )
        cog = AnalyticsCog(
            Mock(),
            SimpleNamespace(quizcord_bot_id=None),
            database,
        )
        cog._refresh_quiz_dashboard = AsyncMock()

        with (
            patch(
                "quizcord_bot.cogs.analytics.extract_leaderboard",
                return_value=("final", "results"),
            ),
            patch(
                "quizcord_bot.cogs.analytics.extract_quizcord_id",
                return_value=None,
            ),
            patch(
                "quizcord_bot.cogs.analytics.is_quiz_start_message",
                return_value=False,
            ),
            patch(
                "quizcord_bot.cogs.analytics.is_quizcord_author",
                return_value=True,
            ),
            patch(
                "quizcord_bot.cogs.analytics.parse_quiz_rows",
                return_value=[("1", "student", "100")],
            ),
            patch(
                "quizcord_bot.cogs.analytics.is_final_leaderboard",
                return_value=True,
            ),
        ):
            await cog._handle_quizcord_message(message)

        guild.fetch_members.assert_not_awaited()

    async def test_usn_reminder_command_is_not_registered(self) -> None:
        self.assertFalse(hasattr(AnalyticsCog, "remind_usn"))

    async def test_quiz_start_dashboard_dm_still_includes_website_link(self) -> None:
        session = SimpleNamespace(
            session_id="session",
            guild_id=1,
            admin_dm_message_id=None,
            admin_user_id=2,
        )
        dashboard_message = SimpleNamespace(
            channel=SimpleNamespace(id=4),
            id=5,
        )
        admin = SimpleNamespace(send=AsyncMock(return_value=dashboard_message))
        guild = SimpleNamespace(owner=admin, owner_id=2)
        database = Mock()
        database.get_quiz_session.return_value = session
        cog = AnalyticsCog(
            Mock(),
            SimpleNamespace(dashboard_public_url="https://analytics.example"),
            database,
        )

        await cog._ensure_quiz_dashboard(guild, "session")

        admin.send.assert_awaited_once()
        self.assertIn("view the live roster", admin.send.await_args.args[0])
        database.set_quiz_dashboard_message.assert_called_once_with(
            "session", 4, 5
        )
