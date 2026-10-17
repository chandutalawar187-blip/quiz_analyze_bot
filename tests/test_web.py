"""Authentication and authorization tests for the web dashboard."""

import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    _is_guild_faculty,
    _oauth_failure_detail,
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
                "FACULTY_ROLE_ID": "33333",
            },
            clear=True,
        ):
            self.settings = Settings.from_environment()
        self.client = TestClient(
            create_dashboard_app(self.settings, self.database),
            base_url="https://testserver",
        )

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

    def test_faculty_access_requires_the_configured_role_id(self) -> None:
        self.assertTrue(_is_guild_faculty({"roles": ["33333"]}, 33333))
        self.assertFalse(_is_guild_faculty({"roles": ["12345"]}, 33333))
        self.assertFalse(_is_guild_faculty({"roles": "33333"}, 33333))
        self.assertFalse(_is_guild_faculty({"roles": ["33333"]}, None))

    def test_oauth_failure_detail_identifies_configuration_stage(self) -> None:
        token_error = _oauth_failure_detail("authorization-code exchange", 400)
        self.assertIn("DASHBOARD_PUBLIC_URL", token_error)
        self.assertIn("/auth/callback", token_error)
        self.assertIn("HTTP 400", token_error)

        faculty_error = _oauth_failure_detail("Faculty role lookup", 403)
        self.assertIn("Faculty role", faculty_error)
        self.assertIn("HTTP 403", faculty_error)

    def test_dashboard_data_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/sessions")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_browser_http_errors_use_discord_style_html_pages(self) -> None:
        missing_page = self.client.get("/missing-page")

        self.assertEqual(missing_page.status_code, 404)
        self.assertIn("text/html", missing_page.headers["content-type"])
        self.assertIn("404", missing_page.text)
        self.assertIn("This page is missing", missing_page.text)
        self.assertIn("Quizcord", missing_page.text)

        unauthorized_page = self.client.get("/students/ABC123")

        self.assertEqual(unauthorized_page.status_code, 401)
        self.assertIn("text/html", unauthorized_page.headers["content-type"])
        self.assertIn("Sign in with Discord", unauthorized_page.text)

    def test_api_http_errors_remain_json(self) -> None:
        response = self.client.get("/api/not-found")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not Found"})

    def test_user_directory_requires_discord_sign_in(self) -> None:
        response = self.client.get("/api/users")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Please sign in again.")

    def test_faculty_dashboard_and_student_apis_require_discord_sign_in(self) -> None:
        for method, path in (
            ("get", "/api/faculty/students"),
            ("get", "/api/faculty/candidates"),
            ("get", "/api/faculty/students/4GM22CS001"),
            ("post", "/api/faculty/students/4GM22CS001"),
            ("delete", "/api/faculty/students/4GM22CS001"),
            ("post", "/api/faculty/password/verify"),
            ("post", "/api/faculty/password/change"),
        ):
            with self.subTest(method=method, path=path):
                options = {"json": {}} if method == "post" else {}
                response = getattr(self.client, method)(path, **options)
                self.assertEqual(response.status_code, 401)

    def test_faculty_page_redirects_to_faculty_discord_login(self) -> None:
        response = self.client.get("/faculty", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/faculty/login")

    def test_faculty_login_pages_are_separate_and_admin_reset_is_protected(self) -> None:
        login_page = self.client.get("/faculty/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("Faculty portal", login_page.text)
        self.assertIn("Continue with Discord", login_page.text)
        self.assertNotIn('type="password"', login_page.text)

        password_page = self.client.get(
            "/faculty/password", follow_redirects=False
        )
        self.assertEqual(password_page.status_code, 302)
        self.assertEqual(password_page.headers["location"], "/faculty/login")
        self.assertEqual(
            self.client.get("/admin/faculty-passwords").status_code,
            401,
        )
        self.assertEqual(
            self.client.post("/api/admin/faculty-passwords/123456").status_code,
            401,
        )

    def test_faculty_can_only_monitor_students_they_added(self) -> None:
        member = SimpleNamespace(
            id=98765,
            name="student_account",
            display_name="4GM22CS001",
            global_name="4GM22CS001",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=SimpleNamespace(id=67890),
            guild_permissions=SimpleNamespace(),
        )
        self.database.sync_member(member)
        self.database.set_faculty_temporary_password(
            67890, 123456, "Temporary!FacultyPassword"
        )
        member_without_usn = SimpleNamespace(
            id=98766,
            name="student_without_usn",
            display_name="Student Without USN",
            global_name="Student Without USN",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=SimpleNamespace(id=67890),
            guild_permissions=SimpleNamespace(),
        )
        self.database.sync_member(member_without_usn)
        member_with_usn_but_invalid_name = SimpleNamespace(
            id=98767,
            name="mapped_student",
            display_name="Mapped Student",
            global_name="Mapped Student",
            status=SimpleNamespace(name="online"),
            bot=False,
            guild=SimpleNamespace(id=67890),
            guild_permissions=SimpleNamespace(),
        )
        self.database.map_student(
            member_with_usn_but_invalid_name,
            "4GM22CS009",
            "Mapped Student",
        )
        self.database.sync_member(member_with_usn_but_invalid_name)

        login = self.client.get(
            "/auth/login?destination=faculty", follow_redirects=False
        )
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        token_response = Mock()
        token_response.json.return_value = {
            "access_token": "faculty-access-token",
            "refresh_token": "faculty-refresh-token",
            "expires_in": 3600,
        }
        user_response = Mock()
        user_response.json.return_value = {
            "id": "123456",
            "global_name": "Faculty Member",
        }
        guilds_response = Mock()
        guilds_response.json.return_value = [
            {"id": "67890", "permissions": "0"}
        ]
        member_response = Mock()
        member_response.json.return_value = {"roles": ["33333"]}
        discord_client = Mock()
        discord_client.post = AsyncMock(return_value=token_response)
        discord_client.get = AsyncMock(
            side_effect=[user_response, guilds_response, member_response]
        )
        client_context = Mock()
        client_context.__aenter__ = AsyncMock(return_value=discord_client)
        client_context.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "quizcord_bot.web.httpx.AsyncClient", return_value=client_context
        ):
            callback = self.client.get(
                f"/auth/callback?code=faculty-code&state={state}",
                follow_redirects=False,
            )

        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback.headers["location"], "/faculty/password")
        member_request = discord_client.get.await_args_list[2]
        self.assertEqual(
            member_request.args[0],
            "https://discord.com/api/v10/guilds/67890/members/123456",
        )
        self.assertEqual(
            member_request.kwargs["headers"]["Authorization"],
            "Bot test-token",
        )
        password_page = self.client.get("/faculty/password")
        self.assertEqual(password_page.status_code, 200)
        self.assertIn("Faculty password", password_page.text)
        self.assertEqual(self.client.get("/faculty").status_code, 200)
        self.assertEqual(
            self.client.get("/api/faculty/students").status_code, 428
        )
        verification = self.client.post(
            "/api/faculty/password/verify",
            json={"password": "Temporary!FacultyPassword"},
        )
        self.assertEqual(verification.status_code, 200)
        self.assertTrue(verification.json()["must_change"])
        self.assertEqual(
            self.client.get("/api/faculty/students").status_code, 428
        )
        change = self.client.post(
            "/api/faculty/password/change",
            json={
                "current_password": "Temporary!FacultyPassword",
                "new_password": "MyNewFacultyPassword123",
            },
        )
        self.assertEqual(change.status_code, 200)
        self.assertTrue(change.json()["changed"])
        self.assertEqual(self.client.get("/faculty").status_code, 200)
        candidates = self.client.get("/api/faculty/candidates").json()
        self.assertEqual(len(candidates), 3)
        candidate_with_usn = next(row for row in candidates if row["usn"])
        candidate_without_usn = next(
            row for row in candidates if row["usn"] is None
        )
        mapped_but_invalid = next(
            row for row in candidates if row["usn"] == "4GM22CS009"
        )
        self.assertTrue(candidate_with_usn["can_assign"])
        self.assertEqual(candidate_without_usn["student_name"], "Student Without USN")
        self.assertFalse(candidate_without_usn["can_assign"])
        self.assertFalse(mapped_but_invalid["can_assign"])
        self.assertFalse(mapped_but_invalid["name_matches_usn_format"])
        searched_candidates = self.client.get(
            "/api/faculty/candidates?search=4GM22"
        ).json()
        self.assertEqual(
            {row["usn"] for row in searched_candidates},
            {"4GM22CS001", "4GM22CS009"},
        )
        rejected_add = self.client.post("/api/faculty/students/4GM22CS009")
        self.assertEqual(rejected_add.status_code, 422)
        self.assertIn("Ask them to update", rejected_add.json()["detail"])
        added = self.client.post("/api/faculty/students/4GM22CS001")
        self.assertEqual(added.status_code, 204)
        students = self.client.get("/api/faculty/students").json()
        self.assertEqual([row["usn"] for row in students], ["4GM22CS001"])
        self.assertEqual(
            self.client.get("/api/faculty/students/NOTASSIGNED").status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/sessions").status_code, 403)

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
        self.assertIn("text/html", callback.headers["content-type"])
        self.assertIn("Invalid sign-in state.", callback.text)

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
