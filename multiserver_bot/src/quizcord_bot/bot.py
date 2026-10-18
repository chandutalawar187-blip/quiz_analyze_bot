"""Discord client setup and application command error handling."""

import asyncio
import logging

import discord
import uvicorn
from discord import app_commands
from discord.ext import commands

from quizcord_bot.cogs.analytics import AnalyticsCog
from quizcord_bot.config import Settings
from quizcord_bot.database import Database
from quizcord_bot.web import create_dashboard_app


LOGGER = logging.getLogger("quizcord_analytics")


class AnalyticsBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )
        self.settings = settings
        self.database = Database(settings.database_path)
        self.dashboard_server: uvicorn.Server | None = None
        self.dashboard_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.database.initialize(
            legacy_guild_id=self.settings.guild_id,
            default_faculty_role_id=self.settings.faculty_role_id,
        )
        if self.settings.dashboard_public_url:
            self.dashboard_server = uvicorn.Server(
                uvicorn.Config(
                    create_dashboard_app(self.settings, self.database),
                    host=self.settings.dashboard_host,
                    port=self.settings.dashboard_port,
                    log_level="info",
                    access_log=False,
                )
            )
            self.dashboard_task = asyncio.create_task(
                self.dashboard_server.serve(),
                name="quizcord-dashboard-server",
            )
            await asyncio.wait_for(self._wait_for_dashboard(), timeout=15)
        cog = AnalyticsCog(self, self.settings, self.database)
        await self.add_cog(cog)
        cog.restore_dashboard_views()
        synced = await self.tree.sync()
        LOGGER.info(
            "Synced %d global application commands.",
            len(synced),
        )
        if self.settings.guild_id is not None:
            legacy_guild = discord.Object(id=self.settings.guild_id)
            self.tree.clear_commands(guild=legacy_guild)
            await self.tree.sync(guild=legacy_guild)
            LOGGER.info(
                "Removed legacy guild-scoped commands from server %s.",
                self.settings.guild_id,
            )

    async def _wait_for_dashboard(self) -> None:
        while self.dashboard_server is not None:
            if self.dashboard_server.started:
                return
            if self.dashboard_task is not None and self.dashboard_task.done():
                await self.dashboard_task
                raise RuntimeError("The dashboard server stopped before startup.")
            await asyncio.sleep(0.05)

    async def close(self) -> None:
        try:
            if self.dashboard_server is not None:
                self.dashboard_server.should_exit = True
            if self.dashboard_task is not None:
                try:
                    await asyncio.wait_for(self.dashboard_task, timeout=10)
                except TimeoutError:
                    self.dashboard_task.cancel()
                    try:
                        await self.dashboard_task
                    except asyncio.CancelledError:
                        pass
                except Exception:
                    LOGGER.exception("The web dashboard server failed during shutdown.")
        finally:
            await super().close()

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Please wait {error.retry_after:.1f} seconds before trying again."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "This command can only be used in a server."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You need the **Manage Server** permission to use this command."
        else:
            LOGGER.error(
                "Application command %s failed.",
                interaction.command.name if interaction.command else "<unknown>",
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "The command could not be completed. Please try again later."

        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
