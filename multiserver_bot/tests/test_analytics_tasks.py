"""Regression tests for quiz dashboard and member notification behavior."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quizcord_bot.cogs.analytics import AnalyticsCog


class QuizNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_update_scans_selected_day_and_reprocesses_messages(
        self,
    ) -> None:
        captured_history_args = {}
        yesterday_message = SimpleNamespace(
            id=1,
            created_at=datetime(2026, 10, 30, 5, tzinfo=timezone.utc),
            author=SimpleNamespace(id=3, bot=True, name="Quizcord"),
            webhook_id=None,
            content="Final leaderboard #1. student_one — 500 Total players 1",
            embeds=[],
        )
        outside_message = SimpleNamespace(
            id=2,
            created_at=datetime(2026, 10, 31, 19, tzinfo=timezone.utc),
            author=SimpleNamespace(id=3, bot=True, name="Quizcord"),
            webhook_id=None,
            content="Final leaderboard #1. student_two — 500 Total players 1",
            embeds=[],
        )

        async def channel_history(**kwargs):
            captured_history_args.update(kwargs)
            for message in (yesterday_message, outside_message):
                yield message

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 31, 10, tzinfo=timezone.utc).astimezone(tz)

        channel = SimpleNamespace(id=12, history=channel_history)
        interaction = SimpleNamespace(
            channel=channel,
            guild_id=1,
            guild=Mock(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        cog = AnalyticsCog(
            Mock(),
            SimpleNamespace(quizcord_bot_id=None),
            Mock(),
        )
        cog._handle_quizcord_message = AsyncMock(return_value="session-1")
        cog._refresh_quiz_dashboard = AsyncMock()
        reset_snapshot_sessions = set()

        with (
            patch("quizcord_bot.cogs.analytics.datetime", FixedDateTime),
            patch(
                "quizcord_bot.cogs.analytics.extract_leaderboard",
                return_value=("quiz", "leaderboard"),
            ),
            patch(
                "quizcord_bot.cogs.analytics.extract_quizcord_id",
                return_value=None,
            ),
        ):
            await AnalyticsCog.history_update.callback(
                cog,
                interaction,
                "2026-10-30",
            )

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        cog._handle_quizcord_message.assert_awaited_once_with(
            yesterday_message,
            history_update=True,
            reset_snapshot_sessions=reset_snapshot_sessions,
            leaderboard_payload="leaderboard",
            quizcord_quiz_id=None,
        )
        self.assertEqual(
            captured_history_args["after"],
            datetime(2026, 10, 29, 18, 30, tzinfo=timezone.utc)
            - timedelta(milliseconds=1),
        )
        self.assertEqual(
            captured_history_args["before"],
            datetime(2026, 10, 30, 18, 30, tzinfo=timezone.utc),
        )
        cog._refresh_quiz_dashboard.assert_awaited_once_with(
            interaction.guild,
            "session-1",
            ensure=False,
        )
        self.assertIn(
            "for 2026-10-30 (IST)",
            interaction.followup.send.await_args.args[0],
        )
        self.assertIn(
            "1 final leaderboard message(s)",
            interaction.followup.send.await_args.args[0],
        )

    async def test_history_update_processes_only_final_leaderboard_until_end(
        self,
    ) -> None:
        day = datetime(2026, 10, 30, 5, tzinfo=timezone.utc)
        author = SimpleNamespace(id=3, bot=True, name="Quizcord")
        messages = [
            SimpleNamespace(
                id=index,
                created_at=day,
                author=author,
                webhook_id=None,
                content=f"Leaderboard update #{index}. student — {index}",
                embeds=[],
            )
            for index in range(1, 24)
        ]
        final_start = SimpleNamespace(
            id=24,
            created_at=day,
            author=author,
            webhook_id=None,
            content="Final leaderboard #96. <@1001>, <@1002>",
            embeds=[],
        )
        final_end = SimpleNamespace(
            id=25,
            created_at=day,
            author=author,
            webhook_id=None,
            content="<@1003>, <@1004> — 0 Total players 246",
            embeds=[],
        )
        messages.extend((final_start, final_end))

        async def channel_history(**kwargs):
            for message in messages:
                yield message

        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 10, 31, 10, tzinfo=timezone.utc).astimezone(tz)

        channel = SimpleNamespace(id=12, history=channel_history)
        interaction = SimpleNamespace(
            channel=channel,
            guild_id=1,
            guild=Mock(),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        cog = AnalyticsCog(
            Mock(),
            SimpleNamespace(quizcord_bot_id=None),
            Mock(),
        )
        cog._handle_quizcord_message = AsyncMock(return_value="session-1")
        cog._refresh_quiz_dashboard = AsyncMock()

        def extracted_leaderboard(message):
            if "leaderboard" in message.content.casefold():
                return "quiz", message.content
            return None

        with (
            patch("quizcord_bot.cogs.analytics.datetime", FixedDateTime),
            patch(
                "quizcord_bot.cogs.analytics.extract_leaderboard",
                side_effect=extracted_leaderboard,
            ),
            patch(
                "quizcord_bot.cogs.analytics.extract_quizcord_id",
                return_value=None,
            ),
        ):
            await AnalyticsCog.history_update.callback(
                cog,
                interaction,
                "2026-10-30",
            )

        cog._handle_quizcord_message.assert_awaited_once_with(
            final_start,
            history_update=True,
            reset_snapshot_sessions=set(),
            leaderboard_payload=(
                "Final leaderboard #96. <@1001>, <@1002>\n"
                "<@1003>, <@1004> — 0 Total players 246"
            ),
            quizcord_quiz_id=None,
        )
        self.assertIn(
            "2 final leaderboard message(s)",
            interaction.followup.send.await_args.args[0],
        )

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
