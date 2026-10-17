"""Authentication and authorization tests for the web dashboard."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from quizcord_bot.config import Settings
from quizcord_bot.database import (
    Database,
    QuizParticipant,
    QuizSession,
    QuizSessionSummary,
    StudentQuizAnalytics,
    StudentQuizRecord,
)
from quizcord_bot.web import (
    _is_guild_admin,
    _summary_detail,
    _student_analytics,
    create_dashboard_app,
)


class DashboardWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "dashboard.db")
        self.database.initialize()
        with patch("quizcord_bot.config.load_dotenv"), patch.dict(
            os.environ,
            {
                "DISCORD_BOT_TOKEN": "test-token",
                "BOT_GUILD_ID": "67890",
                "BOT_DATABASE_PATH": str(
                    Path(self.temp_dir.name) / "dashboard.db"
                ),
                "DASHBOARD_PUBLIC_URL": "https://analytics.example.com",
                "DISCORD_OAUTH_CLIENT_ID": "12345",
                "DISCORD_OAUTH_CLIENT_SECRET": "oauth-secret",
                "DASHBOARD_SESSION_SECRET": "s" * 40,
            },
            clear=True,
        ):
            self.settings = Settings.from_environment()
        self.client = TestClient(create_dashboard_app(self.settings, self.database))

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_admin_access_requires_server_management_permission_or_owner(self) -> None:
        self.assertTrue(
            _is_guild_admin(
                [{"id": "67890", "permissions": str(1 << 5)}], 67890
            )
        )
        self.assertTrue(_is_guild_admin([{"id": "67890", "owner": True}], 67890))
        self.assertFalse(
            _is_guild_admin([{"id": "67890", "permissions": str(1 << 10)}], 67890)
        )
        self.assertFalse(
            _is_guild_admin([{"id": "11111", "owner": True}], 67890)
        )

    def test_dashboard_data_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/sessions")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_user_directory_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/users")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_dashboard_redirects_to_sign_in_and_sets_security_headers(self) -> None:
        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_health_endpoint_does_not_require_sign_in(self) -> None:
        response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_student_analytics_endpoint_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/students/ABC123")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_student_excel_export_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/students/ABC123/export.xlsx")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_student_analytics_serializes_datewise_performance(self) -> None:
        analytics = StudentQuizAnalytics(
            usn="ABC123",
            student_name="Student One",
            quizzes_attended=1,
            completed_quizzes=2,
            attendance_percentage=50.0,
            total_points=900,
            average_points=900.0,
            history=(
                StudentQuizRecord(
                    session_id="session-1",
                    channel_id=2,
                    quizcord_quiz_id="quiz-1",
                    status="completed",
                    started_at="2026-10-01T10:00:00+00:00",
                    completed_at="2026-10-01T10:10:00+00:00",
                    rank=1,
                    points=900,
                ),
            ),
        )

        serialized = _student_analytics(analytics)

        self.assertEqual(serialized["attendance_percentage"], 50.0)
        self.assertEqual(serialized["history"][0]["quizcord_quiz_id"], "quiz-1")
        self.assertEqual(serialized["history"][0]["points"], 900)
        self.assertNotIn("discord_id", serialized)

    def test_oauth_callback_rejects_invalid_state(self) -> None:
        login = self.client.get("/auth/login", follow_redirects=False)
        self.assertEqual(login.status_code, 302)
        self.assertIn("scope=identify+guilds", login.headers["location"])

        callback = self.client.get(
            "/auth/callback?code=not-a-real-code&state=invalid",
            follow_redirects=False,
        )

        self.assertEqual(callback.status_code, 400)
        self.assertEqual(callback.json()["detail"], "Invalid sign-in state.")

    def test_private_dashboard_link_preserves_selected_session_through_login(self) -> None:
        session_id = "a" * 32
        response = self.client.get(
            f"/?session={session_id}", follow_redirects=False
        )
        self.assertEqual(
            response.headers["location"], f"/login?session={session_id}"
        )

        login_page = self.client.get(response.headers["location"])
        self.assertIn(
            f'href="/auth/login?session={session_id}"',
            login_page.text,
        )

    def test_session_json_contains_usns_but_not_internal_discord_mapping(self) -> None:
        session = QuizSession(
            session_id="session-1",
            guild_id=67890,
            channel_id=2,
            status="active",
            question_count=1,
            started_at="2026-10-17T15:00:00+00:00",
            completed_at=None,
            quizcord_quiz_id=None,
            admin_user_id=3,
            admin_dm_channel_id=None,
            admin_dm_message_id=None,
        )
        summary = QuizSessionSummary(
            session=session,
            participants=(
                QuizParticipant(
                    "ABC123",
                    1,
                    800,
                    "private-discord-id",
                    name_matches_usn_format=False,
                ),
            ),
            total_points=800,
            average_points=800.0,
            question_count=1,
        )

        detail = _summary_detail(summary)

        self.assertEqual(detail["participants"][0]["usn"], "ABC123")
        self.assertFalse(detail["participants"][0]["name_matches_usn_format"])
        self.assertNotIn("discord_id", detail["participants"][0])


if __name__ == "__main__":
    unittest.main()
