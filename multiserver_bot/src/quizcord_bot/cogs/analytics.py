"""Commands and event handlers for Quizcord analytics."""

import asyncio
import logging
import sqlite3
from datetime import date, datetime, timezone
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

from quizcord_bot.config import Settings
from quizcord_bot.database import Database, QuizSessionSummary
from quizcord_bot.exports import analytics_workbook
from quizcord_bot.quizcord import (
    extract_leaderboard,
    extract_quizcord_id,
    is_final_leaderboard,
    is_first_question_message,
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
        analytics_count, session_count = await asyncio.to_thread(
            self.database.clear_guild_quiz_history, interaction.guild_id
        )
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

    async def _handle_quizcord_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        try:
            payload = extract_leaderboard(message)
            quiz_id = extract_quizcord_id(message)
            is_start = is_quiz_start_message(message)
            if payload is None and not is_start and quiz_id is None:
                return
            if not is_quizcord_author(
                message.author,
                message.webhook_id,
                self.settings.quizcord_bot_id,
                has_final_leaderboard=(
                    payload is not None or is_start or quiz_id is not None
                ),
            ):
                return
        except ValueError:
            LOGGER.warning("Ignoring oversized Quizcord message %s.", message.id)
            return

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
                summary = self.database.get_quiz_session_summary(
                    recent_session.session_id
                )
                await self._notify_global_name_updates(message.guild, summary)
                await self._refresh_quiz_dashboard(
                    message.guild, recent_session.session_id
                )
            return

        if session is None:
            session = self.database.get_active_quiz_session(message.channel.id)
        if is_start:
            previous_session = session
            session = self.database.start_quiz_session(
                message.guild.id,
                message.channel.id,
                message.guild.owner_id,
                message.created_at.isoformat(),
                force_new=is_first_question_message(message),
                eligible_student_count=self.database.get_human_member_count(
                    message.guild.id
                ),
            )
            if (
                previous_session is not None
                and previous_session.session_id != session.session_id
            ):
                await self._refresh_quiz_dashboard(
                    message.guild, previous_session.session_id
                )
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
            )
            await self._ensure_quiz_dashboard(message.guild, session.session_id)

        if payload is None or session is None:
            return

        _, raw_payload = payload
        try:
            rows = parse_quiz_rows(raw_payload)
        except ValueError:
            LOGGER.warning("Ignoring oversized Quizcord leaderboard %s.", message.id)
            return
        if not rows:
            if quiz_id:
                recent_session = self.database.get_recent_quiz_session(
                    message.channel.id
                )
                if recent_session:
                    self.database.attach_quizcord_id(
                        recent_session.session_id, quiz_id
                    )
                    summary = self.database.get_quiz_session_summary(
                        recent_session.session_id
                    )
                    await self._notify_global_name_updates(
                        message.guild, summary
                    )
                    await self._refresh_quiz_dashboard(
                        message.guild, recent_session.session_id
                    )
                    return
            LOGGER.warning(
                "Quizcord leaderboard message %s had no recognizable result rows (%s).",
                message.id,
                leaderboard_structure(message),
            )
            return

        revision_key = (
            message.edited_at.isoformat()
            if message.edited_at
            else message.created_at.isoformat()
        )
        is_final = is_final_leaderboard(message) or quiz_id is not None
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            message.id,
            revision_key,
            rows,
            is_final,
            quiz_id,
            datetime.now(timezone.utc).isoformat()
            if is_final
            else None,
        )
        if summary is None:
            LOGGER.debug(
                "Quizcord leaderboard revision %s for session %s was already recorded.",
                revision_key,
                session.session_id,
            )
            return
        if is_final:
            await self._notify_global_name_updates(message.guild, summary)
        LOGGER.info(
            "Recorded Quizcord leaderboard revision for session %s: %d rows.",
            session.session_id,
            len(rows),
        )
        await self._refresh_quiz_dashboard(message.guild, session.session_id)

    async def _notify_global_name_updates(
        self, guild: discord.Guild, summary: QuizSessionSummary
    ) -> None:
        for participant in summary.participants:
            if (
                participant.discord_id is None
                or participant.name_matches_usn_format is not False
            ):
                continue
            try:
                member_id = int(participant.discord_id)
            except ValueError:
                LOGGER.warning(
                    "Ignoring invalid Discord ID on quiz participant %s.",
                    participant.usn,
                )
                continue
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.NotFound:
                    continue
                except discord.Forbidden:
                    LOGGER.warning(
                        "Cannot retrieve member %s to send a USN nickname reminder.",
                        member_id,
                    )
                    continue
                except discord.HTTPException:
                    LOGGER.exception(
                        "Could not retrieve member %s for a USN nickname reminder.",
                        member_id,
                    )
                    continue
            usn = self.database.claim_global_name_reminder(
                summary.session.session_id, member_id
            )
            if usn is None:
                continue
            safe_usn = discord.utils.escape_mentions(
                discord.utils.escape_markdown(usn)
            )
            try:
                await member.send(
                    "Your quiz result was recorded. Please set your Discord "
                    f"global display name to your USN: **{safe_usn}**.\n"
                    "For example: `4GM22CS001` or `U22E22CS001`. Do not change "
                    "your unique Discord username. This helps match future "
                    "Quizcord results correctly."
                )
            except discord.Forbidden:
                LOGGER.info(
                    "Could not DM member %s a USN nickname reminder; DMs are unavailable.",
                    member_id,
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Could not DM member %s a USN nickname reminder.",
                    member_id,
                )

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
        self, guild: discord.Guild, session_id: str
    ) -> None:
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
