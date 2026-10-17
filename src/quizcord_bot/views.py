"""Private interactive scorecards for imported quiz results."""

from io import BytesIO

import discord

from quizcord_bot.database import Database
from quizcord_bot.exports import quiz_session_workbook


def result_embed(
    database: Database, usn: str, name: str, quiz_id: str
) -> discord.Embed | None:
    result = database.get_quiz_result(usn, quiz_id)
    if result is None:
        return None

    score, rank = result
    analytics = database.get_student_analytics(usn)
    safe_name = discord.utils.escape_mentions(
        discord.utils.escape_markdown(name or usn)
    )
    safe_usn = discord.utils.escape_mentions(discord.utils.escape_markdown(usn))
    embed = discord.Embed(
        title=f"Quiz scorecard: {safe_name}",
        description=f"Student identifier: **{safe_usn}**",
        color=discord.Color.teal(),
    )
    embed.add_field(name="Leaderboard rank", value=f"#{rank}", inline=True)
    embed.add_field(name="Quiz score", value=score, inline=True)
    if analytics is None:
        embed.set_footer(text=f"Quizcord Analytics • Quiz ID: {quiz_id}")
        return embed
    embed.add_field(
        name="Quizcord quizzes attended",
        value=str(analytics.quizzes_attended),
        inline=True,
    )
    embed.add_field(
        name="Total Quizcord points",
        value=str(analytics.total_points),
        inline=True,
    )
    average_points = analytics.average_points_per_scored_quiz
    embed.add_field(
        name="Average points per scored quiz",
        value=f"{average_points:.2f}" if average_points is not None else "N/A",
        inline=True,
    )
    embed.set_footer(text=f"Quizcord Analytics • Quiz ID: {quiz_id}")
    return embed


class ResultButton(discord.ui.Button):
    def __init__(
        self, database: Database, usn: str, name: str, quiz_id: str
    ) -> None:
        label = discord.utils.escape_mentions(discord.utils.escape_markdown(name))
        super().__init__(
            label=(label or usn)[:80],
            style=discord.ButtonStyle.primary,
        )
        self.database = database
        self.usn = usn
        self.student_name = name
        self.quiz_id = quiz_id

    async def callback(self, interaction: discord.Interaction) -> None:
        embed = result_embed(
            self.database, self.usn, self.student_name, self.quiz_id
        )
        if embed is None:
            await interaction.response.send_message(
                "This student's result is no longer available.", ephemeral=True
            )
            return
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ResultsView(discord.ui.View):
    PAGE_SIZE = 23

    def __init__(
        self, database: Database, profiles: list[dict[str, str]], quiz_id: str
    ) -> None:
        super().__init__(timeout=180)
        self.database = database
        self.profiles = profiles
        self.quiz_id = quiz_id
        self.page = 0
        self._add_page_buttons()

    def _add_page_buttons(self) -> None:
        self.clear_items()
        page_size = self.PAGE_SIZE if len(self.profiles) > 25 else 25
        start = self.page * page_size
        for profile in self.profiles[start : start + page_size]:
            self.add_item(
                ResultButton(
                    self.database, profile["usn"], profile["name"], self.quiz_id
                )
            )
        if len(self.profiles) > 25:
            self.add_item(PreviousPageButton())
            self.add_item(NextPageButton())

    async def show_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        self.page = page
        self._add_page_buttons()
        await interaction.response.edit_message(view=self)


class PreviousPageButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Previous", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ResultsView):
            await view.show_page(interaction, max(0, view.page - 1))


class NextPageButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Next", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ResultsView):
            last_page = (len(view.profiles) - 1) // view.PAGE_SIZE
            await view.show_page(interaction, min(last_page, view.page + 1))


def dashboard_embed(quiz_id: str, count: int) -> discord.Embed:
    embed = discord.Embed(
        title="Quiz results are ready",
        description="Select a student to view their private scorecard.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Scorecards available", value=str(count), inline=True)
    embed.set_footer(text=f"Quizcord Analytics • Quiz ID: {quiz_id}")
    return embed


def quiz_session_embed(database: Database, session_id: str) -> discord.Embed:
    summary = database.get_quiz_session_summary(session_id)
    session = summary.session
    complete = session.status == "completed"
    interrupted = session.status == "interrupted"
    embed = discord.Embed(
        title=(
            "Quiz analytics"
            if complete
            else "Quiz session interrupted"
            if interrupted
            else "Live quiz dashboard"
        ),
        description=(
            "The quiz is complete. Open the analytics tab for the final summary."
            if complete
            else (
                "This quiz session was interrupted by a new quiz."
                if interrupted
                else "Live monitoring is active. The question count follows leaderboard updates."
            )
        ),
        color=(
            discord.Color.green()
            if complete
            else discord.Color.dark_grey()
            if interrupted
            else discord.Color.blurple()
        ),
    )
    progress = (
        f"● Complete · 100% ({summary.question_count} leaderboard updates)"
        if complete
        else (
            f"◌ Interrupted · {summary.question_count} updates"
            if interrupted
            else f"◉ Live · Question {summary.question_count}"
        )
    )
    embed.add_field(name="Quiz progress", value=progress, inline=True)
    embed.add_field(
        name="Students attended" if complete else "Students attending",
        value=str(summary.attendance_count),
        inline=True,
    )
    embed.add_field(
        name="USNs",
        value=", ".join(
            discord.utils.escape_mentions(
                discord.utils.escape_markdown(participant.usn)
            )
            for participant in summary.participants[:20]
        )
        or "Waiting for the first leaderboard",
        inline=False,
    )
    if len(summary.participants) > 20:
        embed.set_footer(text=f"Showing 20 of {len(summary.participants)} students")
    return embed


def quiz_participants_embed(
    database: Database, session_id: str, page: int
) -> discord.Embed:
    summary = database.get_quiz_session_summary(session_id)
    page_size = 20
    page_count = max(1, (len(summary.participants) + page_size - 1) // page_size)
    page = min(max(page, 0), page_count - 1)
    start = page * page_size
    embed = discord.Embed(
        title="Quiz participants",
        description=f"Page {page + 1} of {page_count} · {len(summary.participants)} students",
        color=discord.Color.blurple(),
    )
    for participant in summary.participants[start : start + page_size]:
        safe_usn = discord.utils.escape_mentions(
            discord.utils.escape_markdown(participant.usn)
        )
        embed.add_field(
            name=f"#{participant.rank} · {safe_usn}",
            value=f"{participant.points} points",
            inline=True,
        )
    if not summary.participants:
        embed.description = "No leaderboard participants have been recorded yet."
    return embed


def quiz_analytics_embed(database: Database, session_id: str) -> discord.Embed:
    summary = database.get_quiz_session_summary(session_id)
    embed = discord.Embed(
        title="Final quiz analytics",
        description=(
            "Final leaderboard scores are Quizcord points. Correct-answer counts "
            "are not available from the leaderboard data."
            if summary.session.status == "completed"
            else "Final analytics will be available when Quizcord posts the final leaderboard."
        ),
        color=discord.Color.green()
        if summary.session.status == "completed"
        else discord.Color.gold(),
    )
    embed.add_field(
        name="Status",
        value={
            "completed": "Complete",
            "interrupted": "Interrupted",
        }.get(summary.session.status, "In progress"),
        inline=True,
    )
    embed.add_field(name="Leaderboard updates", value=str(summary.question_count), inline=True)
    embed.add_field(
        name=(
            "Students attended"
            if summary.session.status == "completed"
            else "Students attending"
        ),
        value=str(summary.attendance_count),
        inline=True,
    )
    embed.add_field(name="Combined final points", value=str(summary.total_points), inline=True)
    embed.add_field(
        name="Average final points",
        value=f"{summary.average_points:.2f}",
        inline=True,
    )
    if summary.participants:
        winner = summary.participants[0]
        safe_usn = discord.utils.escape_mentions(
            discord.utils.escape_markdown(winner.usn)
        )
        embed.add_field(
            name="Top rank",
            value=f"#{winner.rank} · {safe_usn} · {winner.points} points",
            inline=False,
        )
    return embed


class QuizDashboardButton(discord.ui.Button):
    def __init__(
        self,
        label: str,
        action: str,
        style: discord.ButtonStyle,
        session_id: str,
    ) -> None:
        super().__init__(
            label=label,
            style=style,
            custom_id=f"quiz-dashboard:{session_id}:{action}",
        )
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:
        if isinstance(self.view, QuizSessionDashboardView):
            await self.view.show(interaction, self.action)


class QuizSessionDashboardView(discord.ui.View):
    def __init__(
        self,
        database: Database,
        session_id: str,
        admin_user_id: int,
        persistent_router: bool = False,
    ) -> None:
        super().__init__(timeout=None)
        self.database = database
        self.session_id = session_id
        self.admin_user_id = admin_user_id
        self.page = 0
        self.tab = "overview"
        self.persistent_router = persistent_router
        self._build_buttons()

    def _build_buttons(self) -> None:
        self.clear_items()
        if self.persistent_router:
            actions = (
                ("Overview", "overview", discord.ButtonStyle.secondary),
                ("Participants", "participants", discord.ButtonStyle.primary),
                ("Analytics", "analytics", discord.ButtonStyle.secondary),
                ("Previous", "previous", discord.ButtonStyle.secondary),
                ("Next", "next", discord.ButtonStyle.secondary),
                ("Export Excel", "export", discord.ButtonStyle.success),
            )
            for label, action, style in actions:
                self.add_item(
                    QuizDashboardButton(label, action, style, self.session_id)
                )
        elif self.tab == "participants":
            self.add_item(
                QuizDashboardButton(
                    "Overview", "overview", discord.ButtonStyle.secondary, self.session_id
                )
            )
            self.add_item(
                QuizDashboardButton(
                    "Previous", "previous", discord.ButtonStyle.secondary, self.session_id
                )
            )
            self.add_item(
                QuizDashboardButton(
                    "Next", "next", discord.ButtonStyle.secondary, self.session_id
                )
            )
            self.add_item(
                QuizDashboardButton(
                    "Analytics", "analytics", discord.ButtonStyle.primary, self.session_id
                )
            )
        elif self.tab == "analytics":
            self.add_item(
                QuizDashboardButton(
                    "Overview", "overview", discord.ButtonStyle.secondary, self.session_id
                )
            )
            self.add_item(
                QuizDashboardButton(
                    "Participants", "participants", discord.ButtonStyle.primary, self.session_id
                )
            )
            if (
                self.database.get_quiz_session_summary(self.session_id).session.status
                == "completed"
            ):
                self.add_item(
                    QuizDashboardButton(
                        "Export Excel",
                        "export",
                        discord.ButtonStyle.success,
                        self.session_id,
                    )
                )
        else:
            self.add_item(
                QuizDashboardButton(
                    "Participants", "participants", discord.ButtonStyle.primary, self.session_id
                )
            )
            self.add_item(
                QuizDashboardButton(
                    "Analytics", "analytics", discord.ButtonStyle.secondary, self.session_id
                )
            )

    async def show(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.user.id != self.admin_user_id:
            await interaction.response.send_message(
                "This dashboard is private to the quiz administrator.",
                ephemeral=True,
            )
            return

        if action == "export":
            summary = self.database.get_quiz_session_summary(self.session_id)
            if summary.session.status != "completed":
                await interaction.response.send_message(
                    "The final quiz export is not available yet.", ephemeral=True
                )
                return
            quiz_id = summary.session.quizcord_quiz_id or summary.session.session_id
            file = discord.File(
                BytesIO(quiz_session_workbook(summary)),
                filename=f"quizcord-{quiz_id}.xlsx",
            )
            await interaction.response.send_message(
                "The final quiz results are attached.", file=file, ephemeral=True
            )
            return

        if action == "previous":
            self.tab = "participants"
            self.page = max(0, self.page - 1)
        elif action == "next":
            self.tab = "participants"
            participant_count = len(
                self.database.get_quiz_session_summary(self.session_id).participants
            )
            last_page = max(0, (participant_count - 1) // 20)
            self.page = min(last_page, self.page + 1)
        else:
            self.tab = action
            if action != "participants":
                self.page = 0
        self._build_buttons()
        if self.tab == "participants":
            embed = quiz_participants_embed(self.database, self.session_id, self.page)
        elif self.tab == "analytics":
            embed = quiz_analytics_embed(self.database, self.session_id)
        else:
            embed = quiz_session_embed(self.database, self.session_id)
        await interaction.response.edit_message(embed=embed, view=self)


class WebDashboardLinkView(discord.ui.View):
    def __init__(self, dashboard_url: str) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="Open live web dashboard",
                style=discord.ButtonStyle.link,
                url=dashboard_url,
            )
        )
