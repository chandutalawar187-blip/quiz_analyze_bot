"""Commands and event handlers for Quizcord analytics."""

import asyncio
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

from quizcord_bot.config import Settings
from quizcord_bot.database import (
    Database,
)
from quizcord_bot.exports import analytics_workbook
from quizcord_bot.quizcord import (
    extract_leaderboard,
    extract_quizcord_id,
    is_final_leaderboard,
    is_quizcord_author,
    is_quiz_start_message,
    leaderboard_structure,
    parse_quiz_rows,
)
from quizcord_bot.views import (
    QuizSessionDashboardView,
    ResultsView,
    WebDashboardLinkView,
    dashboard_embed,
    quiz_session_embed,
)


LOGGER = logging.getLogger("quizcord_analytics")
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


class AnalyticsCog(commands.Cog):
    def __init__(
        self, bot: commands.Bot, settings: Settings, database: Database
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.database = database

    def restore_dashboard_views(self) -> None:
        if self.settings.dashboard_public_url:
            return
        for session in self.database.get_dashboard_sessions():
            self.bot.add_view(
                QuizSessionDashboardView(
                    self.database,
                    session.session_id,
                    session.admin_user_id,
                    persistent_router=True,
                )
            )

    @app_commands.command(
        name="map_student",
        description="Link a Discord member and optional Quizcord nickname to a student identifier.",
    )
    @app_commands.describe(
        quizcord_username="Optional Quizcord leaderboard nickname, with or without @"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(3, 60)
    async def map_student(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        usn: str,
        real_name: str,
        quizcord_username: str | None = None,
    ) -> None:
        normalized_usn = usn.strip().upper()
        normalized_name = real_name.strip()
        if not normalized_usn or len(normalized_usn) > 100:
            await interaction.response.send_message(
                "Enter a student identifier between 1 and 100 characters.",
                ephemeral=True,
            )
            return
        if not normalized_name or len(normalized_name) > 100:
            await interaction.response.send_message(
                "Enter a student name between 1 and 100 characters.",
                ephemeral=True,
            )
            return
        normalized_quizcord_username = (
            quizcord_username.strip().removeprefix("@").strip()
            if quizcord_username
            else None
        )
        if normalized_quizcord_username is not None and (
            not normalized_quizcord_username
            or len(normalized_quizcord_username) > 100
        ):
            await interaction.response.send_message(
                "Enter a Quizcord nickname between 1 and 100 characters.",
                ephemeral=True,
            )
            return

        try:
            self.database.map_student(
                member,
                normalized_usn,
                normalized_name,
                normalized_quizcord_username,
                guild_id=interaction.guild_id,
            )
        except sqlite3.IntegrityError:
            LOGGER.info("Student mapping conflict for Discord member %s.", member.id)
            await interaction.response.send_message(
                "That student identifier or Quizcord nickname is already assigned to another student.",
                ephemeral=True,
            )
            return

        safe_name = discord.utils.escape_mentions(member.name)
        safe_usn = discord.utils.escape_mentions(
            discord.utils.escape_markdown(normalized_usn)
        )
        await interaction.response.send_message(
            (
                f"Linked **{safe_name}** to student identifier **{safe_usn}**."
                + (
                    " Quizcord nickname linked."
                    if normalized_quizcord_username
                    else " For Quizcord matching, add the optional Quizcord nickname "
                    "shown on its leaderboard."
                )
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="process_quizcord",
        description="Import a Quizcord leaderboard and privately view results.",
    )
    @app_commands.describe(
        quiz_id="Unique identifier for this quiz session",
        raw_payload="Paste the leaderboard text from Quizcord",
        quiz_date="Optional date override; otherwise the tracked quiz start time is used",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(3, 60)
    async def process_quizcord(
        self,
        interaction: discord.Interaction,
        quiz_id: str,
        raw_payload: str,
        quiz_date: str | None = None,
    ) -> None:
        normalized_quiz_id = quiz_id.strip()
        if not normalized_quiz_id or len(normalized_quiz_id) > 100:
            await interaction.response.send_message(
                "Enter a quiz identifier between 1 and 100 characters.",
                ephemeral=True,
            )
            return
        normalized_quiz_date: str | None = None
        if quiz_date and quiz_date.strip():
            date_value = quiz_date.strip()
            try:
                parsed_date = date.fromisoformat(date_value)
            except ValueError:
                await interaction.response.send_message(
                    "Enter the quiz date in YYYY-MM-DD format, for example 2026-10-17.",
                    ephemeral=True,
                )
                return
            if parsed_date.isoformat() != date_value:
                await interaction.response.send_message(
                    "Enter the quiz date in YYYY-MM-DD format, for example 2026-10-17.",
                    ephemeral=True,
                )
                return
            normalized_quiz_date = parsed_date.isoformat()

        try:
            rows = parse_quiz_rows(raw_payload)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        profiles = self.database.save_quiz_results(
            normalized_quiz_id,
            rows,
            include_unmatched=True,
            quiz_date=normalized_quiz_date,
            guild_id=interaction.guild_id,
        )
        if not profiles:
            await interaction.response.send_message(
                "No student results were found in the leaderboard.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=dashboard_embed(normalized_quiz_id, len(profiles)),
            view=ResultsView(
                self.database,
                interaction.guild_id,
                profiles,
                normalized_quiz_id,
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="history_update",
        description="Re-read Quizcord results from today or yesterday in this channel.",
    )
    @app_commands.describe(
        date="Choose which day's Quizcord leaderboard history to refresh."
    )
    @app_commands.choices(
        date=[
            app_commands.Choice(name="Today", value="today"),
            app_commands.Choice(name="Yesterday", value="yesterday"),
        ]
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 60)
    async def history_update(
        self,
        interaction: discord.Interaction,
        date: app_commands.Choice[str],
    ) -> None:
        channel = interaction.channel
        history = getattr(channel, "history", None)
        if history is None:
            await interaction.response.send_message(
                "This command requires a text channel or thread with message history.",
                ephemeral=True,
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command could not identify the current server.",
                ephemeral=True,
            )
            return

        target_day = datetime.now(IST).date()
        if date.value == "yesterday":
            target_day -= timedelta(days=1)
        day_start = datetime.combine(target_day, time.min, tzinfo=IST)
        day_end = day_start + timedelta(days=1)
        await interaction.response.defer(ephemeral=True)

        updated_sessions: set[str] = set()
        reset_snapshot_sessions: set[str] = set()
        candidate_messages = 0
        oversized_messages = 0
        try:
            async for message in history(
                after=day_start - timedelta(milliseconds=1),
                before=day_end,
                oldest_first=True,
                limit=None,
            ):
                if message.created_at.astimezone(IST).date() != target_day:
                    continue
                try:
                    payload = extract_leaderboard(message)
                    quiz_id = extract_quizcord_id(message)
                except ValueError:
                    oversized_messages += 1
                    LOGGER.warning(
                        "Skipping oversized Quizcord message %s during history update.",
                        message.id,
                    )
                    continue
                if payload is None and quiz_id is None:
                    continue
                if not is_quizcord_author(
                    message.author,
                    message.webhook_id,
                    self.settings.quizcord_bot_id,
                    has_final_leaderboard=(
                        payload is not None or quiz_id is not None
                    ),
                ):
                    continue
                candidate_messages += 1
                session_id = await self._handle_quizcord_message(
                    message,
                    history_update=True,
                    reset_snapshot_sessions=reset_snapshot_sessions,
                )
                if session_id is not None:
                    updated_sessions.add(session_id)
        except discord.Forbidden:
            LOGGER.warning(
                "Cannot read channel history for history update in guild %s, channel %s.",
                interaction.guild_id,
                getattr(channel, "id", None),
            )
            await interaction.followup.send(
                "I could not read this channel's history. Please grant the bot "
                "View Channel and Read Message History permissions, then retry.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            LOGGER.exception(
                "Discord failed while reading history for guild %s, channel %s.",
                interaction.guild_id,
                getattr(channel, "id", None),
            )
            await interaction.followup.send(
                "Discord could not finish reading this channel's history. "
                f"{len(updated_sessions)} session(s) may already have been refreshed; "
                "please retry after the Discord issue clears.",
                ephemeral=True,
            )
            return
        except sqlite3.Error:
            LOGGER.exception(
                "Could not update saved quiz history for guild %s, channel %s.",
                interaction.guild_id,
                getattr(channel, "id", None),
            )
            await interaction.followup.send(
                "A database error interrupted the history update. Some sessions "
                "may already have been refreshed; contact an administrator "
                "before retrying if the dashboard appears inconsistent.",
                ephemeral=True,
            )
            return

        if not candidate_messages:
            oversized_note = (
                f" {oversized_messages} oversized message(s) were skipped."
                if oversized_messages
                else ""
            )
            await interaction.followup.send(
                f"No Quizcord leaderboard messages were found for {target_day.isoformat()} "
                f"in this channel.{oversized_note}",
                ephemeral=True,
            )
            return

        if not updated_sessions:
            await interaction.followup.send(
                "Quizcord messages were found, but no leaderboard results could be "
                "updated. Check the bot logs for details.",
                ephemeral=True,
            )
            return

        for session_id in updated_sessions:
            await self._refresh_quiz_dashboard(
                guild,
                session_id,
                ensure=False,
            )
        await interaction.followup.send(
            f"Refreshed {len(updated_sessions)} quiz session(s) from "
            f"{candidate_messages} Quizcord message(s) for {target_day.isoformat()} "
            "(IST)."
            + (
                f" Skipped {oversized_messages} oversized message(s)."
                if oversized_messages
                else ""
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="export_analytics",
        description="Download a private Excel workbook of student analytics.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(2, 60)
    async def export_analytics(self, interaction: discord.Interaction) -> None:
        report = self.database.get_analytics_report(interaction.guild_id)
        if not report:
            await interaction.response.send_message(
                "There are no student analytics available to export.", ephemeral=True
            )
            return

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        file = discord.File(
            BytesIO(analytics_workbook(report)),
            filename=f"quizcord-analytics-{timestamp}.xlsx",
        )
        await interaction.response.send_message(
            content=(
                f"Private Excel analytics for {len(report)} students. "
                "Performance values are Quizcord points, not correct-answer counts."
            ),
            file=file,
            ephemeral=True,
        )

    @app_commands.command(
        name="clear_quiz_history",
        description="Permanently clear this server's saved quiz results and sessions.",
    )
    @app_commands.describe(
        confirmation='Type "DELETE" to confirm clearing this server’s quiz data.'
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 300)
    async def clear_quiz_history(
        self,
        interaction: discord.Interaction,
        confirmation: str,
    ) -> None:
        if confirmation != "DELETE":
            await interaction.response.send_message(
                'No data was deleted. To confirm, run this command again and '
                'enter exactly "DELETE".',
                ephemeral=True,
            )
            return
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            analytics_count, session_count = await asyncio.to_thread(
                self.database.clear_guild_quiz_history, interaction.guild_id
            )
        except Exception:
            LOGGER.exception(
                "Could not clear quiz history for guild %s.",
                interaction.guild_id,
            )
            await interaction.followup.send(
                "Could not clear this server’s quiz history. No success was "
                "reported; please retry later or contact an administrator "
                "if the problem continues.",
                ephemeral=True,
            )
            return
        LOGGER.warning(
            "Cleared quiz history for guild %s: %d analytics rows and "
            "%d quiz sessions.",
            interaction.guild_id,
            analytics_count,
            session_count,
        )
        await interaction.followup.send(
            f"Cleared this server’s quiz history: {session_count:,} sessions "
            f"and {analytics_count:,} saved student results. Student mappings "
            "and faculty settings were kept. This cannot be undone.",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            self.database.register_guild(
                guild.id,
                guild.name,
                (
                    self.settings.faculty_role_id
                    if guild.id == self.settings.guild_id
                    else None
                ),
            )
            try:
                members = [
                    member async for member in guild.fetch_members(limit=None)
                ]
            except discord.HTTPException:
                LOGGER.exception("Could not refresh members for guild %s.", guild.id)
                continue
            self.database.sync_members(members)
            LOGGER.info("Refreshed the member cache for guild %s.", guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        self.database.register_guild(
            guild.id,
            guild.name,
            (
                self.settings.faculty_role_id
                if guild.id == self.settings.guild_id
                else None
            ),
        )
        try:
            members = [member async for member in guild.fetch_members(limit=None)]
        except discord.HTTPException:
            LOGGER.exception("Could not refresh members for guild %s.", guild.id)
            return
        self.database.sync_members(members)

    @commands.Cog.listener()
    async def on_guild_update(
        self, before: discord.Guild, after: discord.Guild
    ) -> None:
        if before.name != after.name:
            self.database.register_guild(after.id, after.name)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        self.database.unregister_guild(guild.id)

    @app_commands.command(
        name="set_faculty_role",
        description="Configure which server role can use the faculty dashboard.",
    )
    @app_commands.describe(
        role="Faculty role; leave blank to disable faculty dashboard access"
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_faculty_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
            return
        try:
            self.database.set_faculty_role(
                interaction.guild_id, role.id if role else None
            )
        except LookupError:
            await interaction.response.send_message(
                "This server is not registered yet. Try again in a moment.",
                ephemeral=True,
            )
            return
        message = (
            f"Faculty dashboard access is enabled for {role.mention}."
            if role
            else "Faculty dashboard access is disabled for this server."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        self.database.sync_member(member)

    @commands.Cog.listener()
    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        if (
            before.name != after.name
            or before.display_name != after.display_name
            or before.global_name != after.global_name
        ):
            self.database.sync_member(after)
        elif before.guild_permissions != after.guild_permissions:
            self.database.sync_member(after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        self.database.remove_member(member)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._handle_quizcord_message(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        await self._handle_quizcord_message(after)

    async def _handle_quizcord_message(
        self,
        message: discord.Message,
        *,
        history_update: bool = False,
        reset_snapshot_sessions: set[str] | None = None,
    ) -> str | None:
        if message.guild is None:
            return None
        try:
            payload = extract_leaderboard(message)
            quiz_id = extract_quizcord_id(message)
            is_start = is_quiz_start_message(message)
            if payload is None and not is_start and quiz_id is None:
                return None
            if not is_quizcord_author(
                message.author,
                message.webhook_id,
                self.settings.quizcord_bot_id,
                has_final_leaderboard=(
                    payload is not None or is_start or quiz_id is not None
                ),
            ):
                return None
        except ValueError:
            LOGGER.warning("Ignoring oversized Quizcord message %s.", message.id)
            return None

        session = self.database.get_quiz_session_for_message(
            message.channel.id, message.id
        )
        if quiz_id and payload is None:
            recent_session = (
                session
                or self.database.get_recent_quiz_session(message.channel.id)
            )
            if recent_session:
                self.database.attach_quizcord_id(
                    recent_session.session_id, quiz_id
                )
                if not history_update:
                    await self._refresh_quiz_dashboard(
                        message.guild, recent_session.session_id
                    )
                return recent_session.session_id
            return None

        if session is None:
            session = self.database.get_active_quiz_session(message.channel.id)
        if is_start:
            previous_session = session
            session = self.database.start_quiz_session(
                message.guild.id,
                message.channel.id,
                message.guild.owner_id,
                message.created_at.isoformat(),
                eligible_student_count=self.database.get_human_member_count(
                    message.guild.id
                ),
                quizcord_quiz_id=quiz_id,
            )
            if (
                previous_session is not None
                and previous_session.session_id != session.session_id
                and not history_update
            ):
                await self._refresh_quiz_dashboard(
                    message.guild, previous_session.session_id
                )
            if not history_update:
                await self._ensure_quiz_dashboard(message.guild, session.session_id)
        elif session is None and payload is not None:
            session = self.database.start_quiz_session(
                message.guild.id,
                message.channel.id,
                message.guild.owner_id,
                message.created_at.isoformat(),
                eligible_student_count=self.database.get_human_member_count(
                    message.guild.id
                ),
                quizcord_quiz_id=quiz_id,
            )
            if not history_update:
                await self._ensure_quiz_dashboard(message.guild, session.session_id)

        if payload is None or session is None:
            return None

        _, raw_payload = payload
        try:
            rows = parse_quiz_rows(raw_payload)
        except ValueError:
            LOGGER.warning("Ignoring oversized Quizcord leaderboard %s.", message.id)
            return None
        if not rows:
            if quiz_id:
                recent_session = self.database.get_recent_quiz_session(
                    message.channel.id
                )
                if recent_session:
                    self.database.attach_quizcord_id(
                        recent_session.session_id, quiz_id
                    )
                    await self._refresh_quiz_dashboard(
                        message.guild, recent_session.session_id
                    )
                    return recent_session.session_id
            LOGGER.warning(
                "Quizcord leaderboard message %s had no recognizable result rows (%s).",
                message.id,
                leaderboard_structure(message),
            )
            return None

        revision_key = (
            message.edited_at.isoformat()
            if message.edited_at
            else message.created_at.isoformat()
        )
        is_final = is_final_leaderboard(message) or quiz_id is not None
        completed_at = None
        if is_final:
            completed_at = (
                session.completed_at or message.created_at.isoformat()
                if history_update and session.status == "completed"
                else datetime.now(timezone.utc).isoformat()
            )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            message.id,
            revision_key,
            rows,
            is_final,
            quiz_id,
            completed_at,
            replace_existing=history_update,
            reset_participants=(
                history_update
                and reset_snapshot_sessions is not None
                and session.session_id not in reset_snapshot_sessions
            ),
        )
        if summary is None:
            LOGGER.debug(
                "Quizcord leaderboard revision %s for session %s was already recorded.",
                revision_key,
                session.session_id,
            )
            return None
        if history_update and reset_snapshot_sessions is not None:
            reset_snapshot_sessions.add(session.session_id)
        LOGGER.info(
            "Recorded Quizcord leaderboard revision for session %s: %d rows.",
            session.session_id,
            len(rows),
        )
        if not history_update:
            await self._refresh_quiz_dashboard(message.guild, session.session_id)
        return session.session_id

    async def _quiz_admin(
        self, guild: discord.Guild
    ) -> discord.User | discord.Member | None:
        owner = guild.owner
        if owner is not None:
            return owner
        try:
            return await guild.fetch_member(guild.owner_id)
        except discord.HTTPException:
            LOGGER.exception("Could not retrieve the guild owner for quiz dashboard.")
            return None

    async def _ensure_quiz_dashboard(
        self, guild: discord.Guild, session_id: str
    ) -> None:
        session = self.database.get_quiz_session(session_id)
        if session is None or session.admin_dm_message_id is not None:
            return
        admin = await self._quiz_admin(guild)
        if admin is None:
            return
        try:
            if self.settings.dashboard_public_url:
                dashboard_url = (
                    f"{self.settings.dashboard_public_url}/"
                    f"?guild_id={session.guild_id}&session={session_id}"
                )
                message = await admin.send(
                    "A Quizcord session is being monitored. Sign in with Discord "
                    "to view the live roster and analytics.",
                    view=WebDashboardLinkView(dashboard_url),
                )
            else:
                message = await admin.send(
                    embed=quiz_session_embed(self.database, session_id),
                    view=QuizSessionDashboardView(
                        self.database, session_id, session.admin_user_id
                    ),
                )
        except discord.Forbidden:
            LOGGER.warning(
                "Could not DM the quiz dashboard to the guild owner; DMs are unavailable."
            )
            return
        except discord.HTTPException:
            LOGGER.exception("Could not deliver the live quiz dashboard.")
            return
        self.database.set_quiz_dashboard_message(
            session_id, message.channel.id, message.id
        )

    async def _refresh_quiz_dashboard(
        self,
        guild: discord.Guild,
        session_id: str,
        *,
        ensure: bool = True,
    ) -> None:
        if ensure:
            await self._ensure_quiz_dashboard(guild, session_id)
        if self.settings.dashboard_public_url:
            return
        session = self.database.get_quiz_session(session_id)
        if (
            session is None
            or session.admin_dm_channel_id is None
            or session.admin_dm_message_id is None
        ):
            return
        channel = self.bot.get_channel(session.admin_dm_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(session.admin_dm_channel_id)
            except discord.HTTPException:
                LOGGER.exception("Could not retrieve the admin dashboard DM channel.")
                return
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            LOGGER.error("Stored dashboard channel is not a messageable DM channel.")
            return
        try:
            dashboard_message = await fetch_message(session.admin_dm_message_id)
            await dashboard_message.edit(
                embed=quiz_session_embed(self.database, session_id),
                view=QuizSessionDashboardView(
                    self.database, session_id, session.admin_user_id
                ),
            )
        except discord.Forbidden:
            LOGGER.warning(
                "Could not update the private quiz dashboard message."
            )
        except discord.HTTPException:
            LOGGER.exception("Could not update the private quiz dashboard.")
