"""Persistence and attendance tests."""

import asyncio
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from quizcord_bot.cogs.analytics import AnalyticsCog
from quizcord_bot.database import Database, matches_usn_format
from quizcord_bot.quizcord import parse_quiz_rows
from quizcord_bot.views import result_embed


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "results.db")
        self.database.initialize()
        self.database.register_guild(67890, "Test server")
        self.member = SimpleNamespace(
            id=12345,
            name="Student_One",
            display_name="Student Nickname",
            status=SimpleNamespace(name="online"),
            bot=False,
        )
        self.database.map_student(self.member, "ABC123", "Student One")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_attendance_counts_distinct_quizzes(self) -> None:
        self.database.save_quiz_results(
            "quiz-1",
            [("1", "student_one", "950"), ("1", "student_one", "960")],
        )
        self.database.save_quiz_results(
            "quiz-2",
            [("3", "STUDENT NICKNAME", "800")],
        )

        self.assertEqual(self.database.count_attended_quizzes("ABC123"), 2)
        self.assertEqual(
            self.database.get_quiz_result("ABC123", "quiz-1"),
            ("960 Points", "1"),
        )

    def test_student_quiz_analytics_uses_completed_sessions_and_datewise_history(
        self,
    ) -> None:
        first = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            first.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
            "quiz-oct-1",
            "2026-10-01T10:10:00+00:00",
        )
        missed = self.database.start_quiz_session(
            67890, 1002, 12345, "2026-10-02T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            missed.session_id,
            5002,
            "revision-1",
            [("1", "OTHER_STUDENT", "700")],
            True,
            "quiz-oct-2",
            "2026-10-02T10:10:00+00:00",
        )
        current = self.database.start_quiz_session(
            67890, 1003, 12345, "2026-10-03T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            current.session_id,
            5003,
            "revision-1",
            [("2", "Student Nickname", "650")],
            False,
        )

        analytics = self.database.get_student_quiz_analytics(67890, "abc123")

        self.assertEqual(analytics.usn, "abc123")
        self.assertEqual(analytics.student_name, "Student One")
        self.assertEqual(analytics.quizzes_attended, 1)
        self.assertEqual(analytics.completed_quizzes, 2)
        self.assertEqual(analytics.attendance_percentage, 50.0)
        self.assertEqual(analytics.total_points, 900)
        self.assertEqual(analytics.average_points, 900)
        self.assertEqual(
            [(record.status, record.points) for record in analytics.history],
            [("active", 650), ("completed", 900)],
        )

    def test_student_mappings_and_analytics_are_scoped_to_each_server(self) -> None:
        self.database.register_guild(98765, "Another server", 99999)
        self.database.map_student(
            self.member,
            "ABC123",
            "Student Two",
            guild_id=98765,
        )
        for guild_id, score in ((67890, "900"), (98765, "400")):
            session = self.database.start_quiz_session(
                guild_id,
                guild_id + 1,
                12345,
                "2026-10-05T10:00:00+00:00",
            )
            self.database.record_quiz_leaderboard(
                session.session_id,
                guild_id + 2,
                "revision-1",
                [("1", "Student Nickname", score)],
                True,
                "shared-quiz-id",
            )

        first_server = self.database.get_student_quiz_analytics(67890, "ABC123")
        second_server = self.database.get_student_quiz_analytics(98765, "ABC123")

        self.assertEqual(first_server.student_name, "Student One")
        self.assertEqual(first_server.total_points, 900)
        self.assertEqual(second_server.student_name, "Student Two")
        self.assertEqual(second_server.total_points, 400)
        self.assertEqual(
            self.database.get_quiz_result(
                "ABC123", "shared-quiz-id", guild_id=67890
            ),
            ("900 Points", "1"),
        )
        self.assertEqual(
            self.database.get_quiz_result(
                "ABC123", "shared-quiz-id", guild_id=98765
            ),
            ("400 Points", "1"),
        )

    def test_clear_guild_quiz_history_preserves_other_guild_and_mappings(self) -> None:
        self.database.register_guild(98765, "Another server")
        other_member = SimpleNamespace(
            id=54321,
            name="Student_Two",
            display_name="Student Two",
            status=SimpleNamespace(name="online"),
            bot=False,
        )
        self.database.map_student(
            other_member, "XYZ789", "Student Two", guild_id=98765
        )
        sessions = {}
        for guild_id, channel_id, quiz_id, nickname in (
            (67890, 1001, "college-quiz", "Student Nickname"),
            (98765, 1002, "other-quiz", "Student Two"),
        ):
            session = self.database.start_quiz_session(
                guild_id, channel_id, 12345, "2026-10-05T10:00:00+00:00"
            )
            sessions[guild_id] = session
            self.database.record_quiz_leaderboard(
                session.session_id,
                channel_id + 100,
                "revision-1",
                [("1", nickname, "900")],
                True,
                quiz_id,
            )
        self.database.save_quiz_results(
            "manual-college-quiz",
            [("1", "Student Nickname", "700")],
            guild_id=67890,
        )

        deleted_analytics, deleted_sessions = (
            self.database.clear_guild_quiz_history(67890)
        )

        self.assertEqual(deleted_analytics, 2)
        self.assertEqual(deleted_sessions, 1)
        self.assertEqual(
            self.database.get_student_quiz_analytics(67890, "ABC123").history,
            (),
        )
        self.assertEqual(
            self.database.get_student_quiz_analytics(98765, "XYZ789").total_points,
            900,
        )
        self.assertIsNone(
            self.database.get_quiz_session(sessions[67890].session_id)
        )
        self.assertIsNotNone(
            self.database.get_quiz_session(sessions[98765].session_id)
        )
        self.assertEqual(
            self.database.get_student_quiz_analytics(67890, "ABC123").student_name,
            "Student One",
        )
        with self.database._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_updates"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_participants"
                ).fetchone()[0],
                1,
            )

    def test_student_profile_includes_manual_imports_with_recorded_import_dates(
        self,
    ) -> None:
        self.database.save_quiz_results(
            "manual-quiz-1", [("1", "Student Nickname", "700")]
        )
        self.database.save_quiz_results(
            "manual-quiz-2", [("2", "Student Nickname", "800")]
        )

        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")

        self.assertEqual(analytics.quizzes_attended, 2)
        self.assertEqual(analytics.completed_quizzes, 2)
        self.assertEqual(analytics.attendance_percentage, 100.0)
        self.assertEqual(analytics.total_points, 1500)
        self.assertEqual(len(analytics.history), 2)
        self.assertTrue(all(record.status == "historical" for record in analytics.history))
        self.assertTrue(all(record.started_at is not None for record in analytics.history))
        for record in analytics.history:
            if record.started_at:
                datetime.fromisoformat(record.started_at)

    def test_manual_import_saves_supplied_quiz_date(self) -> None:
        self.database.save_quiz_results(
            "manual-dated-quiz",
            [("1", "Student Nickname", "850")],
            quiz_date="2026-10-12",
        )

        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")

        self.assertEqual(analytics.history[0].started_at, "2026-10-12")

    def test_manual_import_uses_tracked_quiz_start_timestamp(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-12T14:35:00+00:00"
        )
        other_guild_session = self.database.start_quiz_session(
            98765, 2001, 54321, "2026-10-12T16:35:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "850")],
            True,
            "tracked-quiz",
        )
        self.database.record_quiz_leaderboard(
            other_guild_session.session_id,
            5002,
            "revision-1",
            [("1", "Student Nickname", "850")],
            True,
            "tracked-quiz",
        )

        self.database.save_quiz_results(
            "tracked-quiz",
            [("1", "Student Nickname", "850")],
            guild_id=67890,
        )

        with self.database._connect() as connection:
            row = connection.execute(
                "SELECT quiz_date FROM quiz_analytics WHERE quiz_id = ?",
                ("tracked-quiz",),
            ).fetchone()
        self.assertEqual(row[0], "2026-10-12T14:35:00+00:00")

    def test_manual_import_and_tracked_session_with_same_quiz_id_count_once(
        self,
    ) -> None:
        self.database.save_quiz_results(
            "quiz-shared", [("1", "Student Nickname", "950")]
        )
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "950")],
            True,
            "quiz-shared",
        )

        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")

        self.assertEqual(analytics.quizzes_attended, 1)
        self.assertEqual(analytics.completed_quizzes, 1)
        self.assertEqual(analytics.total_points, 950)
        self.assertEqual(len(analytics.history), 1)
        self.assertEqual(analytics.history[0].started_at, "2026-10-01T10:00:00+00:00")

    def test_student_quiz_analytics_includes_unmapped_leaderboard_names(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("4", "UNKNOWN_USN", "725")],
            True,
            "quiz-unknown",
            "2026-10-01T10:10:00+00:00",
        )

        analytics = self.database.get_student_quiz_analytics(
            67890, "UNKNOWN_USN"
        )

        self.assertEqual(analytics.student_name, "UNKNOWN_USN")
        self.assertEqual(analytics.quizzes_attended, 1)
        self.assertEqual(analytics.attendance_percentage, 100.0)
        self.assertEqual(analytics.history[0].points, 725)

    def test_edited_final_leaderboard_reuses_original_quiz_session(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
        )

        edited_session = self.database.get_quiz_session_for_message(1001, 5001)
        self.assertIsNotNone(edited_session)
        self.assertEqual(edited_session.session_id, session.session_id)
        self.database.record_quiz_leaderboard(
            edited_session.session_id,
            5001,
            "revision-2",
            [("1", "Student Nickname", "900")],
            True,
            "quiz-123",
        )

        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")
        self.assertEqual(analytics.quizzes_attended, 1)
        self.assertEqual(len(analytics.history), 1)
        self.assertEqual(analytics.history[0].quizcord_quiz_id, "quiz-123")

    def test_repeated_start_signal_keeps_active_quiz_until_final_leaderboard(
        self,
    ) -> None:
        started_at = "2026-10-24T19:00:00+00:00"
        session = self.database.start_quiz_session(
            67890, 1001, 12345, started_at
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "question-1",
            [("1", "Student Nickname", "900")],
            False,
        )

        repeated_start = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T19:05:00+00:00"
        )
        self.assertEqual(repeated_start.session_id, session.session_id)
        self.assertEqual(repeated_start.status, "active")

        completed = self.database.record_quiz_leaderboard(
            session.session_id,
            5002,
            "final",
            [("1", "Student Nickname", "950")],
            True,
            "quiz-2026-10-24",
        )
        self.assertIsNotNone(completed)
        next_session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T20:00:00+00:00"
        )
        self.assertNotEqual(next_session.session_id, session.session_id)
        self.assertEqual(next_session.status, "active")

    def test_stale_active_session_is_interrupted_before_next_quiz(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T19:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "question-1",
            [("1", "Student Nickname", "900")],
            False,
        )

        next_session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T20:00:00+00:00"
        )

        self.assertNotEqual(next_session.session_id, session.session_id)
        self.assertEqual(next_session.status, "active")
        previous_summary = self.database.get_quiz_session_summary(
            session.session_id
        )
        self.assertEqual(previous_summary.session.status, "interrupted")
        self.assertEqual(previous_summary.session.question_count, 1)
        self.assertEqual(previous_summary.participants[0].points, 900)

    def test_recent_leaderboard_keeps_long_running_quiz_active(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T19:00:00+00:00"
        )
        activity_time = datetime.fromisoformat("2026-10-24T19:50:00+00:00")
        activity_snowflake = (
            int(activity_time.timestamp() * 1000) - 1_420_070_400_000
        ) << 22
        self.database.record_quiz_leaderboard(
            session.session_id,
            activity_snowflake,
            "question-1",
            [("1", "Student Nickname", "900")],
            False,
        )

        repeated_start = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T20:30:00+00:00"
        )
        next_quiz = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-24T20:51:00+00:00"
        )

        self.assertEqual(repeated_start.session_id, session.session_id)
        self.assertNotEqual(next_quiz.session_id, session.session_id)
        self.assertEqual(
            self.database.get_quiz_session_summary(
                session.session_id
            ).session.status,
            "interrupted",
        )

    def test_initialize_preserves_ambiguous_completed_same_day_sessions(
        self,
    ) -> None:
        sessions = []
        for index, (score, quiz_id) in enumerate(
            ((900, None), (950, None), (1000, "3228337"), (1100, None))
        ):
            session = self.database.start_quiz_session(
                67890,
                1001,
                12345,
                f"2026-10-24T19:{index:02}:00+00:00",
            )
            self.database.set_quiz_dashboard_message(
                session.session_id, 9999, 6000 + index
            )
            self.database.record_quiz_leaderboard(
                session.session_id,
                5001 + index,
                f"revision-{index}",
                [("1", "Student Nickname", str(score))],
                True,
                quiz_id,
                f"2026-10-24T19:{index:02}:30+00:00",
            )
            sessions.append(session)

        self.database.initialize()
        self.database.initialize()

        summaries = self.database.get_quiz_session_summaries(67890)
        self.assertEqual(len(summaries), 4)
        self.assertEqual(
            {summary.session.session_id for summary in summaries},
            {session.session_id for session in sessions},
        )
        self.assertTrue(
            all(summary.session.status == "completed" for summary in summaries)
        )
        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")
        self.assertEqual(analytics.quizzes_attended, 4)
        self.assertEqual(analytics.completed_quizzes, 4)
        self.assertEqual(analytics.total_points, 3950)
        with self.database._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_updates"
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM quiz_analytics
                    WHERE guild_id = ? AND usn = ? AND quiz_id IS NOT NULL
                    """,
                    ("67890", "ABC123"),
                ).fetchone()[0],
                4,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_aliases"
                ).fetchone()[0],
                0,
            )
        analytics_count, session_count = self.database.clear_guild_quiz_history(
            67890
        )
        self.assertEqual(session_count, 4)
        self.assertGreater(analytics_count, 0)
        with self.database._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_aliases"
                ).fetchone()[0],
                0,
            )

    def test_initialize_merges_only_sessions_with_shared_uncompleted_message(
        self,
    ) -> None:
        sessions = []
        for index in range(2):
            session = self.database.start_quiz_session(
                67890,
                1001,
                12345,
                f"2026-10-24T19:0{index}:00+00:00",
            )
            self.database.record_quiz_leaderboard(
                session.session_id,
                5001,
                f"revision-{index}",
                [("1", "Student Nickname", str(900 + index * 50))],
                False,
                "same-quiz-id",
            )
            with self.database._connect() as connection:
                connection.execute(
                    "UPDATE quiz_sessions SET status = 'interrupted' "
                    "WHERE session_id = ?",
                    (session.session_id,),
                )
            sessions.append(session)

        self.database.initialize()

        summaries = self.database.get_quiz_session_summaries(67890)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].session.status, "interrupted")
        self.assertEqual(summaries[0].session.question_count, 2)
        self.assertEqual(summaries[0].participants[0].points, 950)
        with self.database._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_aliases"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM quiz_session_updates"
                ).fetchone()[0],
                2,
            )

    def test_initialize_keeps_completed_sessions_even_with_shared_message(
        self,
    ) -> None:
        sessions = []
        for index in range(2):
            session = self.database.start_quiz_session(
                67890,
                1001,
                12345,
                f"2026-10-24T19:0{index}:00+00:00",
            )
            self.database.record_quiz_leaderboard(
                session.session_id,
                5001,
                f"final-{index}",
                [("1", "Student Nickname", str(900 + index * 50))],
                True,
                "same-quiz-id",
            )
            sessions.append(session)

        self.database.initialize()

        summaries = self.database.get_quiz_session_summaries(67890)
        self.assertEqual(len(summaries), 2)
        self.assertEqual(
            {summary.session.session_id for summary in summaries},
            {session.session_id for session in sessions},
        )
        self.assertTrue(
            all(summary.session.status == "completed" for summary in summaries)
        )

    def test_initialize_keeps_sessions_with_different_message_histories(
        self,
    ) -> None:
        for index, message_ids in enumerate(((5001, 5002), (5001, 5003))):
            session = self.database.start_quiz_session(
                67890,
                1001,
                12345,
                f"2026-10-24T19:0{index}:00+00:00",
            )
            for message_id in message_ids:
                self.database.record_quiz_leaderboard(
                    session.session_id,
                    message_id,
                    f"revision-{message_id}",
                    [("1", "Student Nickname", str(message_id))],
                    False,
                    "same-quiz-id",
                )
            with self.database._connect() as connection:
                connection.execute(
                    "UPDATE quiz_sessions SET status = 'interrupted' "
                    "WHERE session_id = ?",
                    (session.session_id,),
                )

        self.database.initialize()

        summaries = self.database.get_quiz_session_summaries(67890)
        self.assertEqual(len(summaries), 2)
        self.assertTrue(
            all(summary.session.status == "interrupted" for summary in summaries)
        )

    def test_initialize_does_not_merge_sessions_with_conflicting_quiz_ids(
        self,
    ) -> None:
        for index, quiz_id in enumerate(("quiz-one", "quiz-two")):
            session = self.database.start_quiz_session(
                67890,
                1001,
                12345,
                f"2026-10-24T19:{index:02}:00+00:00",
            )
            self.database.record_quiz_leaderboard(
                session.session_id,
                5001 + index,
                f"revision-{index}",
                [("1", "Student Nickname", "900")],
                True,
                quiz_id,
            )

        with self.assertLogs("quizcord_analytics.database", level="WARNING"):
            self.database.initialize()

        self.assertEqual(len(self.database.get_quiz_session_summaries(67890)), 2)

    def test_student_history_collapses_legacy_duplicate_session_with_and_without_id(
        self,
    ) -> None:
        started_at = "2026-10-01T10:00:00+00:00"
        without_id = self.database.start_quiz_session(
            67890, 1001, 12345, started_at
        )
        self.database.record_quiz_leaderboard(
            without_id.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
        )
        with_id = self.database.start_quiz_session(
            67890, 1001, 12345, started_at
        )
        self.database.record_quiz_leaderboard(
            with_id.session_id,
            5002,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
            "quiz-123",
        )

        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")

        self.assertEqual(analytics.quizzes_attended, 1)
        self.assertEqual(analytics.completed_quizzes, 1)
        self.assertEqual(analytics.attendance_percentage, 100.0)
        self.assertEqual(len(analytics.history), 1)
        self.assertEqual(analytics.history[0].quizcord_quiz_id, "quiz-123")

    def test_quiz_attendance_includes_all_human_members_regardless_of_permissions(
        self,
    ) -> None:
        guild = SimpleNamespace(id=67890, owner_id=99999)
        self.member.guild = guild
        self.member.guild_permissions = SimpleNamespace(
            administrator=False,
            manage_guild=False,
            manage_roles=False,
            manage_channels=False,
            manage_messages=False,
        )
        privileged_member = SimpleNamespace(
            id=54321,
            name="Moderator",
            display_name="Moderator",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_roles=True,
                manage_channels=False,
                manage_messages=False,
            ),
        )
        owner = SimpleNamespace(
            id=99999,
            name="Owner",
            display_name="Owner",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_roles=False,
                manage_channels=False,
                manage_messages=False,
            ),
        )
        self.database.map_student(privileged_member, "MOD123", "Moderator")
        self.database.sync_members((self.member, privileged_member, owner))
        session = self.database.start_quiz_session(
            guild.id,
            1001,
            owner.id,
            "2026-10-01T10:00:00+00:00",
            eligible_student_count=self.database.get_eligible_member_count(
                guild.id
            ),
        )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900"), ("2", "MOD123", "800")],
            True,
        )

        self.assertEqual(self.database.get_human_member_count(guild.id), 3)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.session.eligible_student_count, 3)
        self.assertEqual(summary.attendance_count, 2)
        self.assertAlmostEqual(summary.attendance_percentage, 66.666, places=2)
        admin_result = next(
            participant
            for participant in summary.participants
            if participant.usn == "MOD123"
        )
        self.assertTrue(admin_result.eligible)
        self.assertFalse(admin_result.name_matches_usn_format)
        self.database.remove_member(self.member)
        self.assertEqual(self.database.get_human_member_count(guild.id), 2)

    def test_dashboard_user_directory_includes_mapped_and_unmapped_humans(self) -> None:
        guild = SimpleNamespace(id=67890)
        valid_member = SimpleNamespace(
            id=12345,
            name="student_account",
            display_name="Server Nickname",
            global_name="4GM22CS001",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        unmapped_member = SimpleNamespace(
            id=54321,
            name="unmapped_account",
            display_name="Student Nickname",
            global_name="Not a USN",
            status=SimpleNamespace(name="offline"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        missing_global_name_member = SimpleNamespace(
            id=67891,
            name="missing_global_name",
            display_name="Server Nickname",
            global_name=None,
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        alternate_format_member = SimpleNamespace(
            id=67892,
            name="alternate_usn_account",
            display_name="Another Nickname",
            global_name="U22E22CS002",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        bot_member = SimpleNamespace(
            id=99999,
            name="quizcord_bot",
            display_name="Quizcord",
            global_name="Quizcord Bot",
            status=SimpleNamespace(name="online"),
            bot=True,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(valid_member, "4GM22CS001", "Student One")
        self.database.sync_members(
            (
                valid_member,
                unmapped_member,
                missing_global_name_member,
                alternate_format_member,
                bot_member,
            )
        )

        users = self.database.get_dashboard_users(guild.id)

        self.assertEqual(len(users), 4)
        mapped_user = next(user for user in users if user.usn)
        self.assertEqual(mapped_user.account_name, "student_account")
        self.assertEqual(mapped_user.global_name, "4GM22CS001")
        self.assertEqual(mapped_user.server_nickname, "Server Nickname")
        self.assertEqual(mapped_user.student_name, "Student One")
        self.assertTrue(mapped_user.name_matches_usn_format)
        unmapped_user = next(
            user for user in users if user.account_name == "unmapped_account"
        )
        self.assertEqual(unmapped_user.account_name, "unmapped_account")
        self.assertEqual(unmapped_user.global_name, "Not a USN")
        self.assertFalse(unmapped_user.name_matches_usn_format)
        missing_name_user = next(
            user for user in users if user.account_name == "missing_global_name"
        )
        self.assertIsNone(missing_name_user.global_name)
        self.assertFalse(missing_name_user.name_matches_usn_format)
        alternate_user = next(
            user for user in users if user.account_name == "alternate_usn_account"
        )
        self.assertEqual(alternate_user.usn, "U22E22CS002")
        self.assertIsNone(alternate_user.student_name)
        self.assertTrue(alternate_user.name_matches_usn_format)

    def test_quiz_session_absentees_exclude_attending_members_and_bots(self) -> None:
        guild = SimpleNamespace(id=67890)
        attendee = SimpleNamespace(
            id=12345,
            name="attendee_account",
            display_name="Attendee Nickname",
            global_name="4GM22CS001",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        absent = SimpleNamespace(
            id=54321,
            name="absent_account",
            display_name="Absent Nickname",
            global_name="4GM22CS002",
            status=SimpleNamespace(name="offline"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        bot = SimpleNamespace(
            id=99999,
            name="quizcord_bot",
            display_name="Quizcord",
            global_name="Quizcord Bot",
            status=SimpleNamespace(name="online"),
            bot=True,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(attendee, "4GM22CS001", "Attendee Student")
        self.database.sync_members((attendee, absent, bot))
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-25T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Attendee Nickname", "900")],
            False,
        )

        absentees = self.database.get_quiz_session_absentees(session.session_id)

        self.assertEqual(len(absentees), 1)
        self.assertEqual(absentees[0].display_name, "Absent Nickname")
        self.assertEqual(absentees[0].usn, "4GM22CS002")

    def test_quiz_session_absentees_sort_by_the_displayed_name(self) -> None:
        guild = SimpleNamespace(id=67890)
        first = SimpleNamespace(
            id=54321,
            name="first_account",
            display_name="Zulu Nickname",
            global_name="A Global Name",
            status=SimpleNamespace(name="offline"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        second = SimpleNamespace(
            id=54322,
            name="second_account",
            display_name="Alpha Nickname",
            global_name="Z Global Name",
            status=SimpleNamespace(name="offline"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.sync_members((first, second))
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-25T10:00:00+00:00"
        )

        absentees = self.database.get_quiz_session_absentees(session.session_id)

        self.assertEqual(
            [student.display_name for student in absentees],
            ["Alpha Nickname", "Zulu Nickname"],
        )

    def test_faculty_student_assignments_are_private_and_require_a_known_usn(
        self,
    ) -> None:
        guild = SimpleNamespace(id=67890)
        member = SimpleNamespace(
            id=54321,
            name="student_account",
            display_name="Manual Student Nickname",
            global_name="4GM22CS001",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.sync_member(member)
        member_with_invalid_global_name = SimpleNamespace(
            id=54322,
            name="invalid_global_name",
            display_name="Student Display",
            global_name="Student Name",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(
            member_with_invalid_global_name, "4GM22CS002", "Student Two"
        )
        self.database.sync_member(member_with_invalid_global_name)

        self.assertTrue(self.database.add_faculty_student(67890, 111, "4gm22cs001"))
        self.assertFalse(self.database.add_faculty_student(67890, 111, "4GM22CS002"))
        self.assertFalse(self.database.add_faculty_student(67890, 111, "4GM22CS001"))
        self.assertEqual(
            self.database.get_faculty_student_usns(67890, 111),
            ("4GM22CS001",),
        )
        self.assertEqual(self.database.get_faculty_student_usns(67890, 222), ())
        self.assertEqual(self.database.get_faculty_student_usns(99999, 111), ())
        self.assertFalse(self.database.add_faculty_student(67890, 111, "NOT-A-USN"))
        self.assertFalse(
            self.database.remove_faculty_student(67890, 222, "4GM22CS001")
        )
        self.assertTrue(
            self.database.remove_faculty_student(67890, 111, "4gm22cs001")
        )
        self.assertEqual(self.database.get_faculty_student_usns(67890, 111), ())

    def test_faculty_temporary_password_requires_change_and_locks_after_failures(
        self,
    ) -> None:
        self.database.set_faculty_temporary_password(67890, 111, "temporary-secret")

        self.assertTrue(self.database.has_faculty_password(67890, 111))
        self.assertEqual(
            self.database.verify_faculty_password(
                67890, 111, "temporary-secret"
            ),
            (True, True),
        )
        self.assertEqual(
            self.database.get_faculty_password_failed_attempts(67890, 111), 0
        )
        self.assertEqual(
            self.database.verify_faculty_password(67890, 111, "wrong-password"),
            (False, False),
        )
        self.assertEqual(
            self.database.get_faculty_password_failed_attempts(67890, 111), 1
        )
        self.database.change_faculty_password(
            67890, 111, "a-new-long-faculty-password"
        )
        self.assertEqual(
            self.database.verify_faculty_password(
                67890, 111, "a-new-long-faculty-password"
            ),
            (True, False),
        )

        self.database.set_faculty_temporary_password(67890, 222, "temp-secret")
        for _ in range(5):
            self.database.verify_faculty_password(67890, 222, "wrong-password")
        self.assertEqual(
            self.database.verify_faculty_password(67890, 222, "temp-secret"),
            (False, False),
        )

    def test_unmapped_privileged_mention_uses_cached_name_and_is_counted(
        self,
    ) -> None:
        guild = SimpleNamespace(id=67890, owner_id=99999)
        eligible_student = SimpleNamespace(
            id=12345,
            name="Student_One",
            display_name="Student Nickname",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_roles=False,
                manage_channels=False,
                manage_messages=False,
            ),
        )
        administrator = SimpleNamespace(
            id=99999,
            name="ServerAdmin",
            display_name="Server Administrator",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=True,
                manage_guild=True,
                manage_roles=True,
                manage_channels=True,
                manage_messages=True,
            ),
        )
        self.database.sync_members((eligible_student, administrator))
        session = self.database.start_quiz_session(
            guild.id,
            1001,
            administrator.id,
            "2026-10-01T10:00:00+00:00",
            eligible_student_count=self.database.get_eligible_member_count(
                guild.id
            ),
        )

        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "<@99999>", "1000")],
            True,
        )

        self.assertEqual(self.database.get_human_member_count(guild.id), 2)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.attendance_count, 1)
        self.assertEqual(summary.attendance_percentage, 50.0)
        self.assertEqual(summary.participants[0].usn, "Server Administrator")
        self.assertEqual(summary.participants[0].discord_id, "99999")
        self.assertTrue(summary.participants[0].eligible)
        self.assertFalse(summary.participants[0].name_matches_usn_format)

    def test_bot_account_is_excluded_but_human_results_count(self) -> None:
        guild = SimpleNamespace(id=67890, owner_id=99999)
        self.member.guild = guild
        self.member.guild_permissions = SimpleNamespace(
            administrator=False,
            manage_guild=False,
            manage_roles=False,
            manage_channels=False,
            manage_messages=False,
        )
        bot_member = SimpleNamespace(
            id=77777,
            name="Quizcord",
            display_name="Quizcord Bot",
            status=SimpleNamespace(name="online"),
            bot=True,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_roles=False,
                manage_channels=False,
                manage_messages=False,
            ),
        )
        self.database.sync_members((self.member, bot_member))
        session = self.database.start_quiz_session(
            guild.id,
            1001,
            12345,
            "2026-10-01T10:00:00+00:00",
            eligible_student_count=self.database.get_human_member_count(guild.id),
        )

        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900"), ("2", "Quizcord Bot", "100")],
            True,
        )

        self.assertEqual(self.database.get_human_member_count(guild.id), 1)
        self.assertIsNotNone(summary)
        self.assertEqual(summary.attendance_count, 1)
        bot_result = next(
            participant
            for participant in summary.participants
            if participant.discord_id == str(bot_member.id)
        )
        self.assertFalse(bot_result.eligible)

    def test_usn_format_accepts_both_configured_patterns(self) -> None:
        self.assertTrue(matches_usn_format("4GM22CS001"))
        self.assertTrue(matches_usn_format("U22E22CS001"))
        self.assertTrue(matches_usn_format("U26E\u200b01CS027"))
        self.assertTrue(matches_usn_format("4GM23CS055\u200b"))
        self.assertTrue(matches_usn_format("４ＧＭ２３ＣＳ０５５"))
        self.assertFalse(matches_usn_format("Student Nickname"))
        self.assertFalse(matches_usn_format("4GM22CS01"))

    def test_usn_warning_checks_global_name_not_server_nickname(self) -> None:
        guild = SimpleNamespace(id=67890, owner_id=99999)
        self.member.guild = guild
        self.member.guild_permissions = SimpleNamespace(
            administrator=False,
            manage_guild=False,
            manage_roles=False,
            manage_channels=False,
            manage_messages=False,
        )
        self.member.global_name = "4GM22CS\u200b001"
        self.database.sync_member(self.member)
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
        )

        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        self.assertTrue(summary.participants[0].name_matches_usn_format)

    def test_valid_leaderboard_usn_is_not_warned_when_global_name_differs(self) -> None:
        guild = SimpleNamespace(id=67890)
        member = SimpleNamespace(
            id=54321,
            name="student_account",
            display_name="Manual Student Nickname",
            global_name="Student Name",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(member, "4GM22CS055", "Student")
        self.database.sync_member(member)
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student", "900")],
            True,
        )

        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        participant = summary.participants[0]
        self.assertEqual(participant.usn, "4GM22CS055")
        self.assertTrue(participant.name_matches_usn_format)
        self.assertFalse(participant.global_name_matches_usn_format)

    def test_faculty_assignment_accepts_unicode_formatted_usn(self) -> None:
        member = SimpleNamespace(
            id=54321,
            name="student_account",
            display_name="Student Nickname",
            global_name="4GM22CS\u200b001",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=SimpleNamespace(id=67890),
            guild_permissions=SimpleNamespace(),
        )
        self.database.sync_member(member)

        self.assertTrue(
            self.database.add_faculty_student(67890, 111, "4GM22CS001")
        )

    def test_global_name_reminder_is_limited_to_once_per_registered_student_per_quiz(
        self,
    ) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        self.assertEqual(
            self.database.claim_global_name_reminder(session.session_id, 12345),
            "ABC123",
        )
        self.assertIsNone(
            self.database.claim_global_name_reminder(session.session_id, 12345)
        )
        self.assertIsNone(
            self.database.claim_global_name_reminder(session.session_id, 54321)
        )

    def test_global_name_reminder_can_use_quiz_usn_without_student_mapping(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        self.assertEqual(
            self.database.claim_global_name_reminder(
                session.session_id, 54321, "4GM22CS055"
            ),
            "4GM22CS055",
        )
        self.assertIsNone(
            self.database.claim_global_name_reminder(
                session.session_id, 54321, "4GM22CS055"
            )
        )
        self.database.release_global_name_reminder(
            session.session_id, 54321
        )
        self.assertEqual(
            self.database.claim_global_name_reminder(
                session.session_id, 54321, "4GM22CS055"
            ),
            "4GM22CS055",
        )

    def test_reminder_dms_linked_member_without_student_mapping(self) -> None:
        guild = SimpleNamespace(id=67890, name="Test server")
        member = SimpleNamespace(
            id=54321,
            name="unmapped_account",
            display_name="Unmapped Nickname",
            global_name="Student Name",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False, manage_guild=False
            ),
            roles=[],
            send=AsyncMock(),
        )
        self.database.sync_member(member)
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Unmapped Nickname", "900")],
            True,
        )
        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        guild.get_member = lambda member_id: member
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        result = asyncio.run(cog._notify_global_name_updates(guild, summary))

        self.assertEqual(result.sent, 1)
        self.assertEqual(result.unavailable, 0)
        member.send.assert_awaited_once()
        reminder = member.send.await_args.args[0]
        self.assertIn("set your Discord global display name to your USN.", reminder)

    def test_final_quiz_sends_a_private_global_name_reminder_once(self) -> None:
        guild_record = SimpleNamespace(
            id=67890,
            owner_id=99999,
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
                manage_roles=False,
                manage_channels=False,
                manage_messages=False,
            ),
        )
        self.member.guild = guild_record
        self.member.guild_permissions = guild_record.guild_permissions
        self.member.global_name = "Student One"
        self.database.sync_member(self.member)
        session = self.database.start_quiz_session(
            guild_record.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
        )
        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        discord_member = SimpleNamespace(
            id=12345,
            global_name="Student One",
            send=AsyncMock(),
            guild_permissions=SimpleNamespace(
                administrator=False,
                manage_guild=False,
            ),
            roles=[],
        )
        guild = SimpleNamespace(
            id=67890,
            name="Test server",
            get_member=lambda member_id: discord_member,
        )
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        asyncio.run(cog._notify_global_name_updates(guild, summary))
        asyncio.run(cog._notify_global_name_updates(guild, summary))

        self.assertEqual(discord_member.send.await_count, 1)
        reminder = discord_member.send.await_args.args[0]
        self.assertIn("ABC123", reminder)
        self.assertIn("MultiServer Bot", reminder)
        self.assertIn("Test server", reminder)
        self.assertIn("global display name", reminder)
        self.assertIn("Do not change your unique Discord username", reminder)

    def test_reminder_uses_current_global_name_instead_of_quiz_snapshot(self) -> None:
        guild = SimpleNamespace(id=67890, name="Test server")
        member = self.member
        member.guild = guild
        member.guild_permissions = SimpleNamespace(
            administrator=False, manage_guild=False
        )
        member.global_name = "4GM22CS055"
        member.roles = []
        member.send = AsyncMock()
        self.database.sync_member(member)
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "Student Nickname", "900")],
            True,
        )
        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        self.assertTrue(summary.participants[0].global_name_matches_usn_format)
        member.global_name = "Student One"
        guild.get_member = lambda member_id: member
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        result = asyncio.run(cog._notify_global_name_updates(guild, summary))

        self.assertEqual(result.sent, 1)
        member.send.assert_awaited_once()
        self.assertIn("ABC123", member.send.await_args.args[0])

    def test_final_quiz_usn_reminders_exclude_faculty_and_administrators(self) -> None:
        guild = SimpleNamespace(id=67890, name="Test server")
        faculty_role_id = 33333
        members = (
            SimpleNamespace(
                id=54321,
                name="faculty_account",
                display_name="Faculty Nickname",
                global_name="Faculty Member",
                status=SimpleNamespace(name="online"),
                bot=False,
                guild=guild,
                guild_permissions=SimpleNamespace(
                    administrator=False, manage_guild=False
                ),
                roles=[SimpleNamespace(id=faculty_role_id)],
                send=AsyncMock(),
            ),
            SimpleNamespace(
                id=54322,
                name="admin_account",
                display_name="Admin Nickname",
                global_name="Administrator",
                status=SimpleNamespace(name="online"),
                bot=False,
                guild=guild,
                guild_permissions=SimpleNamespace(
                    administrator=True, manage_guild=True
                ),
                roles=[],
                send=AsyncMock(),
            ),
            SimpleNamespace(
                id=54323,
                name="student_account",
                display_name="Learner Nickname",
                global_name="Student Name",
                status=SimpleNamespace(name="online"),
                bot=False,
                guild=guild,
                guild_permissions=SimpleNamespace(
                    administrator=False, manage_guild=False
                ),
                roles=[],
                send=AsyncMock(),
            ),
        )
        for member, usn, student_name in zip(
            members,
            ("4GM22CS001", "4GM22CS002", "4GM22CS003"),
            ("Faculty", "Administrator", "Student"),
        ):
            self.database.map_student(member, usn, student_name)
        self.database.sync_members(members)
        self.database.set_faculty_role(guild.id, faculty_role_id)
        session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [
                ("1", "Faculty Nickname", "900"),
                ("2", "Admin Nickname", "800"),
                ("3", "Learner Nickname", "700"),
            ],
            True,
        )
        if summary is None:
            self.fail("Expected the final leaderboard summary.")
        self.assertEqual(
            [
                (
                    participant.usn,
                    participant.discord_id,
                    participant.global_name_matches_usn_format,
                )
                for participant in summary.participants
            ],
            [
                ("4GM22CS001", "54321", False),
                ("4GM22CS002", "54322", False),
                ("4GM22CS003", "54323", False),
            ],
        )
        members_by_id = {member.id: member for member in members}
        guild.get_member = members_by_id.get
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        asyncio.run(cog._notify_global_name_updates(guild, summary))

        self.assertEqual(members[0].send.await_count, 0)
        self.assertEqual(members[1].send.await_count, 0)
        self.assertEqual(members[2].send.await_count, 1)

    def test_faculty_can_manually_send_usn_reminders_once_for_latest_completed_quiz(
        self,
    ) -> None:
        guild = SimpleNamespace(id=67890, name="Test server", owner_id=99999)
        faculty_role_id = 33333
        member = SimpleNamespace(
            id=54321,
            name="student_account",
            display_name="Manual Student Nickname",
            global_name="Student Name",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=guild,
            guild_permissions=SimpleNamespace(
                administrator=False, manage_guild=False
            ),
            roles=[],
            send=AsyncMock(),
        )
        self.database.map_student(member, "4GM22CS001", "Student")
        self.database.sync_member(member)
        self.database.set_faculty_role(guild.id, faculty_role_id)
        completed_session = self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            completed_session.session_id,
            5001,
            "revision-1",
            [("1", "Manual Student Nickname", "900")],
            True,
        )
        self.database.start_quiz_session(
            guild.id, 1001, 12345, "2026-10-02T10:00:00+00:00"
        )
        guild.get_member = lambda member_id: member
        faculty = SimpleNamespace(
            id=22222,
            roles=[SimpleNamespace(id=faculty_role_id)],
            guild_permissions=SimpleNamespace(
                administrator=False, manage_guild=False
            ),
        )
        interaction = SimpleNamespace(
            guild_id=guild.id,
            guild=guild,
            channel_id=1001,
            user=faculty,
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        asyncio.run(AnalyticsCog.remind_usn.callback(cog, interaction))
        asyncio.run(AnalyticsCog.remind_usn.callback(cog, interaction))

        self.assertEqual(interaction.response.defer.await_count, 2)
        self.assertEqual(interaction.followup.send.await_count, 2)
        self.assertEqual(member.send.await_count, 1)
        self.assertIn("Sent: 1", interaction.followup.send.call_args_list[0].args[0])
        self.assertIn("Sent: 0", interaction.followup.send.call_args_list[1].args[0])

    def test_usn_reminder_command_rejects_non_faculty_non_admin(self) -> None:
        guild = SimpleNamespace(id=67890, name="Test server", owner_id=99999)
        self.database.set_faculty_role(guild.id, 33333)
        interaction = SimpleNamespace(
            guild_id=guild.id,
            guild=guild,
            channel_id=1001,
            user=SimpleNamespace(
                id=22222,
                roles=[],
                guild_permissions=SimpleNamespace(
                    administrator=False, manage_guild=False
                ),
            ),
            response=SimpleNamespace(
                send_message=AsyncMock(),
                defer=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        cog = AnalyticsCog(
            bot=SimpleNamespace(),
            settings=SimpleNamespace(),
            database=self.database,
        )

        asyncio.run(AnalyticsCog.remind_usn.callback(cog, interaction))

        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(
            interaction.response.send_message.await_args.kwargs["ephemeral"]
        )
        interaction.response.defer.assert_not_awaited()

    def test_analytics_report_aggregates_points_and_attendance(self) -> None:
        self.database.save_quiz_results(
            "quiz-1", [("1", "student nickname", "950")]
        )
        self.database.save_quiz_results(
            "quiz-2", [("2", "student nickname", "850")]
        )

        [student] = self.database.get_analytics_report()

        self.assertEqual(student.discord_id, "12345")
        self.assertEqual(student.quizzes_attended, 2)
        self.assertEqual(student.quizzes_with_points, 2)
        self.assertEqual(student.total_points, 1800)
        self.assertEqual(student.average_points_per_scored_quiz, 900)

    def test_ambiguous_mapped_nickname_is_not_guessed(self) -> None:
        second_member = SimpleNamespace(
            id=67890,
            name="Student_Two",
            display_name="Student Nickname",
            status=SimpleNamespace(name="online"),
            bot=False,
        )
        self.database.map_student(second_member, "DEF456", "Student Two")

        profiles = self.database.save_quiz_results(
            "quiz-1", [("1", "Student Nickname", "500")]
        )

        self.assertEqual(profiles, [])

    def test_matches_at_prefixed_nickname(self) -> None:
        profiles = self.database.save_quiz_results(
            "quiz-at", [("1", "@Student Nickname", "650")]
        )

        self.assertEqual(
            profiles, [{"usn": "ABC123", "name": "Student One"}]
        )

    def test_matches_explicit_quizcord_username(self) -> None:
        self.database.map_student(
            self.member, "ABC123", "Student One", "likith"
        )

        profiles = self.database.save_quiz_results(
            "quiz-handle", [("1", "@Likith", "799")]
        )

        self.assertEqual(
            profiles, [{"usn": "ABC123", "name": "Student One"}]
        )

    def test_duplicate_quizcord_username_is_rejected(self) -> None:
        self.database.map_student(
            self.member, "ABC123", "Student One", "likith"
        )
        second_member = SimpleNamespace(
            id=67890,
            name="Student_Two",
            display_name="Student Two",
            status=SimpleNamespace(name="online"),
            bot=False,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.database.map_student(
                second_member, "DEF456", "Student Two", "@LIKITH"
            )

    def test_matches_discord_user_mention_by_mapped_discord_id(self) -> None:
        profiles = self.database.save_quiz_results(
            "quiz-mention", [("1", "<@!12345>", "700")]
        )

        self.assertEqual(
            profiles, [{"usn": "ABC123", "name": "Student One"}]
        )

    def test_composite_discord_mentions_resolve_to_known_student(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        summary = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [
                (
                    "1",
                    "<@1561031883473551441>, <@!12345>",
                    "2702",
                )
            ],
            True,
        )

        if summary is None:
            self.fail("Expected a final leaderboard summary.")
        self.assertEqual(len(summary.participants), 1)
        self.assertEqual(summary.participants[0].usn, "ABC123")
        self.assertEqual(summary.participants[0].discord_id, "12345")
        self.assertEqual(summary.participants[0].points, 2702)

    def test_grouped_leaderboard_members_are_independent_and_scores_update(self) -> None:
        second_member = SimpleNamespace(
            id=54321,
            name="Student_Two",
            display_name="Student Two",
            global_name="4GM22CS002",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=SimpleNamespace(id=67890),
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(second_member, "DEF456", "Student Two")
        self.database.sync_member(second_member)
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )

        first = self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "question-1",
            parse_quiz_rows("#2. @Student Nickname, <@54321> 7"),
            False,
        )
        updated = self.database.record_quiz_leaderboard(
            session.session_id,
            5002,
            "question-2",
            parse_quiz_rows("#1. <@12345>, <@54321> — **9**"),
            False,
        )
        final = self.database.record_quiz_leaderboard(
            session.session_id,
            5003,
            "final",
            parse_quiz_rows("#1. <@12345>, <@54321> — **9**"),
            True,
            "quiz-grouped",
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(updated)
        self.assertIsNotNone(final)
        for summary in (first, updated, final):
            participants_by_id = {
                participant.discord_id: participant
                for participant in summary.participants
            }
            self.assertEqual(set(participants_by_id), {"12345", "54321"})
        self.assertEqual(
            {
                participant.usn: (participant.rank, participant.points)
                for participant in updated.participants
            },
            {"ABC123": (1, 9), "DEF456": (1, 9)},
        )
        self.assertEqual(
            {
                participant.usn: participant.points
                for participant in final.participants
            },
            {"ABC123": 9, "DEF456": 9},
        )
        self.assertEqual(
            self.database.get_student_quiz_analytics(67890, "ABC123").total_points,
            9,
        )

    def test_startup_repairs_existing_composite_mention_participant(self) -> None:
        session = self.database.start_quiz_session(
            67890, 1001, 12345, "2026-10-01T10:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            5001,
            "revision-1",
            [("1", "LEGACY_UNKNOWN", "2702")],
            True,
            "legacy-quiz",
        )
        legacy_name = "<@1561031883473551441>, <@!12345>"
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE quiz_session_participants
                SET usn = ?, discord_id = NULL
                WHERE session_id = ?
                """,
                (legacy_name, session.session_id),
            )
            connection.execute(
                "UPDATE quiz_analytics SET usn = ? WHERE quiz_id = ?",
                (legacy_name, "legacy-quiz"),
            )

        self.database.initialize()

        summary = self.database.get_quiz_session_summary(session.session_id)
        self.assertEqual(len(summary.participants), 1)
        self.assertEqual(summary.participants[0].usn, "ABC123")
        self.assertEqual(summary.participants[0].discord_id, "12345")
        analytics = self.database.get_student_quiz_analytics(67890, "ABC123")
        self.assertEqual(analytics.quizzes_attended, 1)
        with self.database._connect() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM quiz_analytics WHERE usn = ?",
                    (legacy_name,),
                ).fetchone()
            )

    def test_matches_normalized_unicode_whitespace_alias(self) -> None:
        self.member.display_name = "Student  Nickname"
        self.database.sync_member(self.member)

        profiles = self.database.save_quiz_results(
            "quiz-space", [("1", "@student   nickname", "700")]
        )

        self.assertEqual(
            profiles, [{"usn": "ABC123", "name": "Student One"}]
        )

    def test_member_nickname_changes_refresh_the_mapped_alias(self) -> None:
        self.member.display_name = "Updated Nickname"
        self.database.sync_member(self.member)

        profiles = self.database.save_quiz_results(
            "quiz-1", [("1", "updated nickname", "500")]
        )

        self.assertEqual(
            profiles, [{"usn": "ABC123", "name": "Student One"}]
        )

    def test_automated_message_is_processed_once(self) -> None:
        rows = [("1", "student_one", "950")]
        self.assertEqual(
            self.database.save_quiz_results(
                "quiz-1", rows, source_message_id=67890
            ),
            [{"usn": "ABC123", "name": "Student One"}],
        )
        self.assertIsNone(
            self.database.save_quiz_results(
                "quiz-1", rows, source_message_id=67890
            )
        )
        self.assertEqual(self.database.count_attended_quizzes("ABC123"), 1)

    def test_duel_results_include_rows_without_a_unique_student_mapping(self) -> None:
        profiles = self.database.save_quiz_results(
            "duel-quiz",
            [
                ("1", "Student Nickname", "900"),
                ("2", "DUEL_USN_002", "700"),
                ("3", "Unmapped Student", "500"),
            ],
            include_unmatched=True,
        )

        self.assertEqual(
            profiles,
            [
                {"usn": "ABC123", "name": "Student One"},
                {"usn": "DUEL_USN_002", "name": "DUEL_USN_002"},
                {"usn": "UNMAPPED STUDENT", "name": "UNMAPPED STUDENT"},
            ],
        )
        self.assertEqual(
            self.database.get_quiz_result("DUEL_USN_002", "duel-quiz"),
            ("700 Points", "2"),
        )

    def test_ambiguous_mapping_is_included_by_leaderboard_name_in_duel_mode(
        self,
    ) -> None:
        second_member = SimpleNamespace(
            id=67890,
            name="Student_Two",
            display_name="Student Nickname",
            status=SimpleNamespace(name="online"),
            bot=False,
        )
        self.database.map_student(second_member, "DEF456", "Student Two")

        profiles = self.database.save_quiz_results(
            "duel-ambiguous",
            [("1", "Student Nickname", "650")],
            include_unmatched=True,
        )

        self.assertEqual(
            profiles,
            [{"usn": "STUDENT NICKNAME", "name": "STUDENT NICKNAME"}],
        )

    def test_live_session_counts_distinct_leaderboard_revisions_and_keeps_all_usns(
        self,
    ) -> None:
        session = self.database.start_quiz_session(
            67890, 222, 333, "2026-10-17T15:00:00+00:00"
        )
        first = self.database.record_quiz_leaderboard(
            session.session_id,
            444,
            "2026-10-17T15:01:00+00:00",
            [("1", "Student Nickname", "800"), ("2", "Unknown_USN", "600")],
            False,
        )

        self.assertIsNotNone(first)
        self.assertEqual(first.question_count, 1)
        self.assertEqual(
            {participant.usn for participant in first.participants},
            {"ABC123", "UNKNOWN_USN"},
        )
        self.assertIsNone(
            self.database.record_quiz_leaderboard(
                session.session_id,
                444,
                "2026-10-17T15:01:00+00:00",
                [("1", "Student Nickname", "800"), ("2", "Unknown_USN", "600")],
                False,
            )
        )

        final = self.database.record_quiz_leaderboard(
            session.session_id,
            444,
            "2026-10-17T15:02:00+00:00",
            [("1", "Student Nickname", "950"), ("2", "Unknown_USN", "700")],
            True,
        )

        self.assertIsNotNone(final)
        self.assertEqual(final.session.status, "completed")
        self.assertEqual(final.question_count, 2)
        self.assertEqual(final.total_points, 1650)
        self.database.attach_quizcord_id(session.session_id, "quiz-123")
        self.assertEqual(
            self.database.get_quiz_result("ABC123", "quiz-123"),
            ("950 Points", "1"),
        )
        self.assertEqual(self.database.count_attended_quizzes("ABC123"), 1)

    def test_quiz_id_followup_completes_and_persists_latest_roster(self) -> None:
        session = self.database.start_quiz_session(
            67890, 222, 333, "2026-10-17T15:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            444,
            "revision-1",
            [("1", "Student Nickname", "950"), ("2", "New_USN", "700")],
            False,
        )

        self.database.attach_quizcord_id(session.session_id, "quiz-followup")

        summary = self.database.get_quiz_session_summary(session.session_id)
        self.assertEqual(summary.session.status, "completed")
        self.assertEqual(summary.session.quizcord_quiz_id, "quiz-followup")
        self.assertEqual(
            self.database.get_quiz_result("ABC123", "quiz-followup"),
            ("950 Points", "1"),
        )

    def test_quiz_id_followup_merges_existing_analytics_row(self) -> None:
        session = self.database.start_quiz_session(
            67890, 222, 333, "2026-10-17T15:00:00+00:00"
        )
        self.database.record_quiz_leaderboard(
            session.session_id,
            444,
            "revision-1",
            [("1", "Student Nickname", "950")],
            True,
        )
        self.database.save_quiz_results(
            "quiz-followup",
            [("2", "Student Nickname", "700")],
        )
        with self.database._connect() as connection:
            connection.execute(
                """
                UPDATE quiz_analytics SET quiz_date = ?
                WHERE guild_id = ? AND usn = ? AND quiz_id = ?
                """,
                ("2026-10-17T15:02:00+00:00", "67890", "ABC123", "quiz-followup"),
            )

        self.database.attach_quizcord_id(session.session_id, "quiz-followup")

        self.assertEqual(
            self.database.get_quiz_result("ABC123", "quiz-followup"),
            ("950 Points", "1"),
        )
        self.assertEqual(self.database.count_attended_quizzes("ABC123"), 1)
        with self.database._connect() as connection:
            self.assertIsNone(
                connection.execute(
                    """
                    SELECT 1 FROM quiz_analytics
                    WHERE guild_id = ? AND usn = ? AND quiz_id = ?
                    """,
                    ("67890", "ABC123", session.session_id),
                ).fetchone()
            )
            quiz_date = connection.execute(
                """
                SELECT quiz_date FROM quiz_analytics
                WHERE guild_id = ? AND usn = ? AND quiz_id = ?
                """,
                ("67890", "ABC123", "quiz-followup"),
            ).fetchone()[0]
        self.assertEqual(quiz_date, "2026-10-17T15:02:00+00:00")

    def test_scorecard_shows_quizcord_attendance(self) -> None:
        self.database.save_quiz_results(
            "quiz-1", [("1", "student_one", "950")]
        )
        self.database.save_quiz_results(
            "quiz-2", [("2", "student_one", "850")]
        )

        embed = result_embed(self.database, "ABC123", "Student One", "quiz-1")

        self.assertIsNotNone(embed)
        attendance_field = next(
            field
            for field in embed.fields
            if field.name == "Quizcord quizzes attended"
        )
        self.assertEqual(attendance_field.value, "2")

    def test_unmapped_duel_result_can_open_a_scorecard(self) -> None:
        self.database.save_quiz_results(
            "duel-quiz",
            [("2", "DUEL_USN_002", "700")],
            include_unmatched=True,
        )

        embed = result_embed(
            self.database, "DUEL_USN_002", "DUEL_USN_002", "duel-quiz"
        )

        self.assertIsNotNone(embed)
        self.assertEqual(embed.title, "Quiz scorecard: DUEL\\_USN\\_002")
        self.assertEqual(
            {field.name: field.value for field in embed.fields},
            {"Leaderboard rank": "#2", "Quiz score": "700 Points"},
        )

    def test_startup_removes_retired_admin_password_and_reset_tables(self) -> None:
        connection = sqlite3.connect(self.database.path)
        connection.executescript(
            """
            CREATE TABLE admin_credentials (
                guild_id TEXT NOT NULL,
                discord_id TEXT NOT NULL,
                password_hash TEXT NOT NULL
            );
            CREATE TABLE password_reset_tokens (
                token_hash TEXT PRIMARY KEY
            );
            INSERT INTO admin_credentials VALUES ('67890', '111', 'legacy-hash');
            INSERT INTO password_reset_tokens VALUES ('legacy-token-hash');
            """
        )
        connection.close()

        self.database.initialize()

        connection = sqlite3.connect(self.database.path)
        remaining_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        connection.close()
        self.assertNotIn("admin_credentials", remaining_tables)
        self.assertNotIn("password_reset_tokens", remaining_tables)

    def test_guild_settings_are_isolated_and_leave_preserves_analytics(self) -> None:
        self.database.register_guild(98765, "Another server")
        self.database.set_faculty_role(98765, 55555)

        self.assertEqual(
            self.database.get_installed_guilds(),
            ((98765, "Another server"), (67890, "Test server")),
        )
        self.assertIsNone(self.database.get_faculty_role_id(67890))
        self.assertEqual(self.database.get_faculty_role_id(98765), 55555)

        self.database.unregister_guild(98765)

        self.assertEqual(self.database.get_installed_guilds(), ((67890, "Test server"),))
        self.assertEqual(self.database.get_faculty_role_id(98765), 55555)

    def test_legacy_student_data_requires_an_explicit_migration_server(self) -> None:
        path = Path(self.temp_dir.name) / "legacy-data.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, status TEXT);
            CREATE TABLE student_registry (
                discord_id TEXT PRIMARY KEY,
                usn TEXT UNIQUE,
                student_name TEXT
            );
            INSERT INTO users VALUES ('12345', 'student', 'online');
            INSERT INTO student_registry VALUES ('12345', 'ABC123', 'Student One');
            """
        )
        connection.close()

        with self.assertRaisesRegex(ValueError, "BOT_GUILD_ID"):
            Database(path).initialize()

        connection = sqlite3.connect(path)
        registry = connection.execute(
            "SELECT usn FROM student_registry"
        ).fetchone()
        connection.close()
        self.assertEqual(registry, ("ABC123",))

    def test_existing_registry_schema_is_migrated(self) -> None:
        path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE users (id TEXT PRIMARY KEY, name TEXT, status TEXT);
            CREATE TABLE student_registry (
                discord_id TEXT PRIMARY KEY,
                usn TEXT UNIQUE,
                student_name TEXT
            );
            CREATE TABLE quiz_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usn TEXT,
                quiz_id TEXT,
                accuracy TEXT,
                time_taken TEXT,
                rank TEXT,
                UNIQUE(usn, quiz_id)
            );
            INSERT INTO users VALUES ('12345', 'student', 'online');
            INSERT INTO student_registry (discord_id, usn, student_name)
                VALUES ('12345', 'ABC123', 'Student One');
            INSERT INTO quiz_analytics (
                usn, quiz_id, accuracy, time_taken, rank
            ) VALUES ('ABC123', 'legacy-quiz', '900 Points', 'N/A', '1');
            """
        )
        connection.close()

        Database(path).initialize(legacy_guild_id=67890)

        connection = sqlite3.connect(path)
        registry_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(student_registry)"
            )
        }
        registry = connection.execute(
            "SELECT guild_id, usn FROM student_registry"
        ).fetchone()
        analytics = connection.execute(
            "SELECT guild_id, usn, quiz_id FROM quiz_analytics"
        ).fetchone()
        connection.close()
        self.assertIn("guild_id", registry_columns)
        self.assertIn("discord_username", registry_columns)
        self.assertIn("discord_nickname", registry_columns)
        self.assertIn("quizcord_username", registry_columns)
        self.assertEqual(registry, ("67890", "ABC123"))
        self.assertEqual(analytics, ("67890", "ABC123", "legacy-quiz"))


class ClearQuizHistoryCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_requires_typed_confirmation_before_deleting(self) -> None:
        database = SimpleNamespace(clear_guild_quiz_history=Mock())
        cog = AnalyticsCog(SimpleNamespace(), SimpleNamespace(), database)
        interaction = SimpleNamespace(
            guild_id=67890,
            response=SimpleNamespace(send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.clear_quiz_history.callback(cog, interaction, "delete")

        database.clear_guild_quiz_history.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "No data was deleted",
            interaction.response.send_message.await_args.args[0],
        )

    async def test_clears_only_current_server_after_confirmation(self) -> None:
        database = SimpleNamespace(
            clear_guild_quiz_history=Mock(return_value=(12, 4))
        )
        cog = AnalyticsCog(SimpleNamespace(), SimpleNamespace(), database)
        events = []

        async def defer(**kwargs):
            events.append(("defer", kwargs))

        def clear_history(guild_id):
            events.append(("clear", guild_id))
            return 12, 4

        database.clear_guild_quiz_history.side_effect = clear_history
        interaction = SimpleNamespace(
            guild_id=67890,
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=defer),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(
                send=AsyncMock(
                    side_effect=lambda *args, **kwargs: events.append(
                        ("followup", args, kwargs)
                    )
                )
            ),
        )

        await cog.clear_quiz_history.callback(cog, interaction, "DELETE")

        database.clear_guild_quiz_history.assert_called_once_with(67890)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        self.assertEqual(
            [event[0] for event in events], ["defer", "clear", "followup"]
        )
        self.assertIn(
            "4 sessions and 12 saved student results",
            interaction.followup.send.await_args.args[0],
        )
        self.assertTrue(
            interaction.followup.send.await_args.kwargs["ephemeral"]
        )

    async def test_reports_history_clear_failure_after_deferring(self) -> None:
        database = SimpleNamespace(
            clear_guild_quiz_history=Mock(
                side_effect=sqlite3.OperationalError("database is locked")
            )
        )
        cog = AnalyticsCog(SimpleNamespace(), SimpleNamespace(), database)
        interaction = SimpleNamespace(
            guild_id=67890,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with self.assertLogs(
            "quizcord_analytics", level="ERROR"
        ) as captured:
            await cog.clear_quiz_history.callback(cog, interaction, "DELETE")

        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        self.assertIn(
            "Could not clear this server’s quiz history",
            interaction.followup.send.await_args.args[0],
        )
        self.assertNotIn(
            "Cleared this server’s quiz history",
            interaction.followup.send.await_args.args[0],
        )
        self.assertTrue(
            interaction.followup.send.await_args.kwargs["ephemeral"]
        )
        self.assertIn("database is locked", captured.output[0])


if __name__ == "__main__":
    unittest.main()
