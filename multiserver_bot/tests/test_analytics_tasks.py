"""Automatic reminders must not block quiz dashboard updates."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from quizcord_bot.cogs.analytics import AnalyticsCog


class AutomaticReminderTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_automatic_completion_path_refreshes_while_reminders_wait(self):
        for path in ("quiz-id", "empty-leaderboard", "final-leaderboard"):
            with self.subTest(path=path):
                database = Mock()
                session = SimpleNamespace(session_id="session")
                database.get_quiz_session_for_message.return_value = session
                database.get_recent_quiz_session.return_value = session
                cog = AnalyticsCog(Mock(), SimpleNamespace(quizcord_bot_id=None), database)
                started = asyncio.Event()
                release = asyncio.Event()

                async def delayed_reminders(*args):
                    started.set()
                    await release.wait()

                cog._notify_global_name_updates = AsyncMock(side_effect=delayed_reminders)
                cog._refresh_quiz_dashboard = AsyncMock()
                now = datetime.now(timezone.utc)
                message = SimpleNamespace(guild=Mock(), channel=Mock(), id=123,
                                          author=Mock(), webhook_id=None, edited_at=None, created_at=now,
                                          content="", embeds=[])
                payload = None if path == "quiz-id" else (None, "synthetic results")
                rows = [] if path == "empty-leaderboard" else [("1", "student", "100")]
                with (patch("quizcord_bot.cogs.analytics.extract_leaderboard", return_value=payload),
                      patch("quizcord_bot.cogs.analytics.extract_quizcord_id", return_value="quiz"),
                      patch("quizcord_bot.cogs.analytics.is_quiz_start_message", return_value=False),
                      patch("quizcord_bot.cogs.analytics.is_quizcord_author", return_value=True),
                      patch("quizcord_bot.cogs.analytics.parse_quiz_rows", return_value=rows)):
                    try:
                        await asyncio.wait_for(cog._handle_quizcord_message(message), timeout=1)
                        cog._refresh_quiz_dashboard.assert_awaited_once_with(message.guild, "session")
                        await asyncio.wait_for(started.wait(), timeout=1)
                        self.assertEqual(len(cog._reminder_tasks), 1)
                    finally:
                        release.set()
                        await cog.cog_unload()

    async def test_task_errors_are_logged_and_tasks_are_released(self):
        cog = AnalyticsCog(Mock(), Mock(), Mock())
        cog._notify_global_name_updates = AsyncMock(side_effect=RuntimeError("delivery failed"))
        with self.assertLogs("quizcord_analytics", level="ERROR") as logs:
            cog._schedule_global_name_updates(Mock(), Mock())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.assertFalse(cog._reminder_tasks)
        self.assertIn("delivery failed", "\n".join(logs.output))

    async def test_unloading_cancels_pending_reminders(self):
        cog = AnalyticsCog(Mock(), Mock(), Mock())
        cog._notify_global_name_updates = AsyncMock(side_effect=lambda *args: None)
        cog._schedule_global_name_updates(Mock(), Mock())
        task = next(iter(cog._reminder_tasks))
        await cog.cog_unload()
        self.assertTrue(task.cancelled())
        self.assertFalse(cog._reminder_tasks)
