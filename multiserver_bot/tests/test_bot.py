"""Discord client startup behavior tests."""

from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from quizcord_bot.bot import AnalyticsBot


def _forbidden_missing_access() -> discord.Forbidden:
    response = Mock(status=403, reason="Forbidden", headers={})
    return discord.Forbidden(
        response, {"code": 50001, "message": "Missing Access"}
    )


class AnalyticsBotStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            token="test-token",
            database_path=Path("test.db"),
            guild_id=123456789,
            faculty_role_id=None,
            dashboard_public_url=None,
        )
        with patch("quizcord_bot.bot.Database"):
            self.bot = AnalyticsBot(self.settings)
        self.bot.database = Mock()
        self.bot.add_cog = AsyncMock()
        self.cog = Mock()

    async def test_missing_access_during_legacy_cleanup_does_not_crash(self) -> None:
        self.bot.tree.sync = AsyncMock(
            side_effect=[[], _forbidden_missing_access()]
        )

        with patch("quizcord_bot.bot.AnalyticsCog", return_value=self.cog):
            with self.assertLogs("quizcord_analytics", level="WARNING") as logs:
                await self.bot.setup_hook()

        self.assertEqual(self.bot.tree.sync.await_count, 2)
        self.assertIn("403 Missing Access", logs.output[0])
        self.assertIn(str(self.settings.guild_id), logs.output[0])
        self.bot.add_cog.assert_awaited_once_with(self.cog)

    async def test_global_command_sync_forbidden_still_fails_startup(self) -> None:
        self.bot.tree.sync = AsyncMock(side_effect=_forbidden_missing_access())

        with patch("quizcord_bot.bot.AnalyticsCog", return_value=self.cog):
            with self.assertRaises(discord.Forbidden):
                await self.bot.setup_hook()

        self.bot.tree.sync.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
