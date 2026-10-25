"""Authentication and authorization tests for the web dashboard."""

import base64
import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from quizcord_bot.config import Settings
from quizcord_bot.database import (
    Database,
    QuizParticipant,
    QuizSessionAbsentStudent,
    QuizSession,
    QuizSessionSummary,
    StudentQuizAnalytics,
    StudentQuizRecord,
)
from quizcord_bot.web import (
    _is_guild_admin,
    _is_guild_faculty,
    _oauth_failure_detail,
    _render_admin_dashboard,
    _summary_detail,
    _student_analytics,
    create_dashboard_app,
)


class DashboardWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "dashboard.db")
        self.database.initialize()
        self.database.register_guild(67890, "Test server", 33333)
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
                "TURNSTILE_SITE_KEY": "turnstile-site-key",
                "TURNSTILE_SECRET_KEY": "turnstile-secret-key",
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

    def test_oauth_login_is_rate_limited(self) -> None:
        responses = [
            self.client.get(
                "/auth/login",
                follow_redirects=False,
                headers={"X-Real-IP": "203.0.113.10"},
            )
            for _ in range(12)
        ]

        self.assertTrue(all(response.status_code == 302 for response in responses))
        limited = self.client.get(
            "/auth/login",
            follow_redirects=False,
            headers={"X-Real-IP": "203.0.113.10"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)
        self.assertEqual(limited.headers["X-RateLimit-Limit"], "12")
        self.assertIn("Too many requests", limited.text)
        other_client = self.client.get(
            "/auth/login",
            follow_redirects=False,
            headers={"X-Real-IP": "203.0.113.11"},
        )
        self.assertEqual(other_client.status_code, 302)

    def test_oauth_login_preserves_existing_dashboard_session(self) -> None:
        signer = TimestampSigner(self.settings.dashboard_session_secret)
        original_session = {
            "dashboard_session": "existing-dashboard-session",
            "selected_guild_id": "67890",
        }
        encoded_session = base64.b64encode(
            json.dumps(original_session).encode("utf-8")
        )
        self.client.cookies.set(
            "quizcord_dashboard",
            signer.sign(encoded_session).decode("utf-8"),
        )

        response = self.client.get("/auth/login", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        cookie_header = response.headers["set-cookie"]
        cookie_value = cookie_header.split(";", 1)[0].split("=", 1)[1]
        signed_payload = signer.unsign(cookie_value)
        session_payload = json.loads(
            base64.b64decode(signed_payload).decode("utf-8")
        )
        self.assertEqual(
            session_payload["dashboard_session"],
            "existing-dashboard-session",
        )
        self.assertEqual(session_payload["selected_guild_id"], "67890")
        self.assertIn("oauth_state", session_payload)

    def test_general_page_requests_are_rate_limited(self) -> None:
        with patch("quizcord_bot.web.DEFAULT_REQUEST_RATE_LIMIT", (2, 60)):
            self.assertEqual(self.client.get("/missing-one").status_code, 404)
            self.assertEqual(self.client.get("/missing-two").status_code, 404)
            limited = self.client.get("/missing-three")

        self.assertEqual(limited.status_code, 429)
        self.assertIn("Retry-After", limited.headers)

    def test_faculty_password_provisioning_limit_is_shared_across_user_ids(self) -> None:
        responses = [
            self.client.post(f"/api/admin/faculty-passwords/{discord_id}")
            for discord_id in range(1000, 1010)
        ]

        self.assertTrue(all(response.status_code == 401 for response in responses))
        limited = self.client.post("/api/admin/faculty-passwords/2000")
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["detail"], "Too many requests. Please wait before trying again.")
        self.assertEqual(limited.headers["X-RateLimit-Limit"], "10")

    def test_browser_http_errors_use_discord_style_html_pages(self) -> None:
        missing_page = self.client.get("/missing-page")

        self.assertEqual(missing_page.status_code, 404)
        self.assertIn("text/html", missing_page.headers["content-type"])
        self.assertIn("404", missing_page.text)
        self.assertIn("This page is missing", missing_page.text)
        self.assertIn("Quizcord", missing_page.text)

        unauthorized_page = self.client.get(
            "/students/ABC123", follow_redirects=False
        )

        self.assertEqual(unauthorized_page.status_code, 302)
        self.assertEqual(unauthorized_page.headers["location"], "/login")

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

    def test_admin_sign_in_page_is_distinct_and_preserves_private_session(self) -> None:
        session_id = "0123456789abcdef0123456789abcdef"
        response = self.client.get(f"/login?session={session_id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Administrator workspace", response.text)
        self.assertIn("Admin sign in", response.text)
        self.assertIn(
            "servers where the bot is installed and you have Manage Server permission",
            response.text,
        )
        self.assertIn("Go to the faculty portal", response.text)
        self.assertIn(f'href="/auth/login?session={session_id}"', response.text)

    def test_admin_discord_sign_in_does_not_require_an_app_password(self) -> None:
        self.database.register_guild(98765, "Another server")
        login = self.client.get(
            "/auth/login?destination=faculty", follow_redirects=False
        )
        oauth_query = parse_qs(urlsplit(login.headers["location"]).query)
        state = oauth_query["state"][0]
        self.assertEqual(oauth_query["scope"][0], "identify guilds")

        token_response = Mock()
        token_response.json.return_value = {
            "access_token": "admin-access-token",
            "refresh_token": "admin-refresh-token",
            "expires_in": 3600,
        }
        user_response = Mock()
        user_response.json.return_value = {
            "id": "987654",
            "global_name": "Server Admin",
        }
        guilds_response = Mock()
        guilds_response.json.return_value = [
            {"id": "67890", "permissions": str(1 << 5)},
            {"id": "98765", "permissions": str(1 << 5)},
        ]
        faculty_member_response = Mock()
        faculty_member_response.json.return_value = {"roles": ["33333"]}
        discord_client = Mock()
        discord_client.post = AsyncMock(return_value=token_response)
        discord_client.get = AsyncMock(
            side_effect=[
                user_response,
                guilds_response,
                faculty_member_response,
            ]
        )
        client_context = Mock()
        client_context.__aenter__ = AsyncMock(return_value=discord_client)
        client_context.__aexit__ = AsyncMock(return_value=False)
        with (
            patch(
                "quizcord_bot.web.httpx.AsyncClient",
                return_value=client_context,
            ),
            patch(
                "quizcord_bot.telemetry.sentry_sdk.start_span",
                return_value=nullcontext(),
            ) as start_span,
        ):
            callback = self.client.get(
                f"/auth/callback?code=admin-login&state={state}",
                follow_redirects=False,
            )

        span_names = {
            call.kwargs["name"] for call in start_span.call_args_list
        }
        self.assertTrue(
            {
                "discord.oauth.token_exchange",
                "discord.oauth.current_user",
                "discord.oauth.list_guilds",
                "discord.guild.member_lookup",
            }.issubset(span_names)
        )
        self.assertEqual(callback.status_code, 302)
        self.assertEqual(callback.headers["location"], "/faculty/password")
        self.assertEqual(self.client.get("/faculty/password").status_code, 200)
        faculty_dashboard = self.client.get("/faculty", follow_redirects=False)
        self.assertEqual(faculty_dashboard.status_code, 302)
        self.assertEqual(
            faculty_dashboard.headers["location"], "/faculty/password"
        )
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 200)
        self.assertEqual(self.client.get("/api/sessions").status_code, 200)
        available_guilds = self.client.get("/api/guilds")
        self.assertEqual(
            [guild["id"] for guild in available_guilds.json()],
            ["98765", "67890"],
        )
        guild_access = {
            guild["id"]: (guild["role"], guild["is_faculty"])
            for guild in available_guilds.json()
        }
        self.assertEqual(guild_access["67890"], ("admin", True))
        self.assertEqual(guild_access["98765"], ("admin", False))
        self.assertIs(guild_access["67890"][1], True)
        self.assertIs(guild_access["98765"][1], False)
        selected = self.client.post(
            "/api/guilds/select", json={"guild_id": "98765"}
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(self.client.get("/api/me").json()["guild_id"], "98765")
        missing_faculty_role = self.client.post(
            "/api/admin/faculty-passwords/1550724138765783102"
        )
        self.assertEqual(missing_faculty_role.status_code, 409)
        self.assertEqual(
            missing_faculty_role.json()["detail"],
            "Set this server's faculty role before provisioning passwords "
            "with /set_faculty_role.",
        )
        rejected_guild = self.client.post(
            "/api/guilds/select", json={"guild_id": "11111"}
        )
        self.assertEqual(rejected_guild.status_code, 403)
        self.assertEqual(self.client.get("/admin/password").status_code, 404)
        self.assertEqual(
            self.client.post("/api/admin/password/setup", json={}).status_code,
            404,
        )
        self.assertEqual(self.client.get("/password/reset").status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/password-reset/request", json={"account_type": "admin"}
            ).status_code,
            404,
        )

    def test_faculty_login_pages_are_separate_and_recovery_is_disabled(self) -> None:
        login_page = self.client.get("/faculty/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("FACULTY PORTAL", login_page.text)
        self.assertIn("Continue with Discord", login_page.text)
        self.assertIn('href="/auth/login?destination=faculty"', login_page.text)
        self.assertIn('href="/login"', login_page.text)
        self.assertIn("A clearer view of every learner", login_page.text)
        self.assertIn("Quizcord never asks for or stores your Discord password", login_page.text)
        self.assertIn("Your administrator provides this app password separately", login_page.text)
        self.assertNotIn('type="password"', login_page.text)

        password_page = self.client.get(
            "/faculty/password", follow_redirects=False
        )
        self.assertEqual(password_page.status_code, 302)
        self.assertEqual(password_page.headers["location"], "/faculty/login")
        self.assertEqual(
            self.client.get(
                "/admin/faculty-passwords", follow_redirects=False
            ).headers["location"],
            "/login",
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
            "email": "faculty@example.com",
            "verified": True,
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
        self.assertNotIn("Email me a password reset link", password_page.text)
        self.assertIn("Use 12–128 characters", password_page.text)
        self.assertIn('data-sitekey="turnstile-site-key"', password_page.text)
        self.assertLess(
            password_page.text.index('id="captcha-container"'),
            password_page.text.index('id="password"'),
        )
        self.assertIn(
            'id="password" type="password" autocomplete="current-password" disabled',
            password_page.text,
        )
        self.assertIn(
            'let mustChange=false,turnstileWidgetId=null,captchaToken="",verifiedTemporaryPassword="";',
            password_page.text,
        )
        self.assertIn("password.readOnly=true", password_page.text)
        self.assertIn(
            "current_password:verifiedTemporaryPassword", password_page.text
        )
        self.assertIn(
            "initializeGuildSelector().then(showCaptcha).catch",
            password_page.text,
        )
        self.assertIn(
            '"expired-callback":()=>{if(mustChange)return;captchaToken="";',
            password_page.text,
        )
        self.assertIn(
            '"error-callback":()=>{if(mustChange)return;captchaToken="";',
            password_page.text,
        )
        self.assertEqual(self.client.get("/faculty").status_code, 200)
        self.assertEqual(
            self.client.get("/api/faculty/students").status_code, 428
        )
        self.assertEqual(self.client.get("/password/reset").status_code, 404)
        self.assertEqual(
            self.client.post(
                "/api/password-reset/request",
                json={"account_type": "faculty"},
            ).status_code,
            404,
        )

        missing_captcha = self.client.post(
            "/api/faculty/password/verify",
            json={"password": "Temporary!FacultyPassword"},
        )
        self.assertEqual(missing_captcha.status_code, 428)
        self.assertTrue(missing_captcha.json()["captcha_required"])
        self.assertEqual(
            self.database.get_faculty_password_failed_attempts(67890, 123456),
            0,
        )

        invalid_response = Mock()
        invalid_response.json.return_value = {
            "success": True,
            "hostname": "untrusted.example.com",
            "action": "faculty_password",
        }
        turnstile_client = Mock()
        turnstile_client.post = AsyncMock(return_value=invalid_response)
        turnstile_context = Mock()
        turnstile_context.__aenter__ = AsyncMock(return_value=turnstile_client)
        turnstile_context.__aexit__ = AsyncMock(return_value=False)
        with patch(
            "quizcord_bot.web.httpx.AsyncClient",
            return_value=turnstile_context,
        ):
            invalid_captcha = self.client.post(
                "/api/faculty/password/verify",
                json={
                    "password": "Temporary!FacultyPassword",
                    "turnstile_token": "invalid-token",
                },
            )
        self.assertEqual(invalid_captcha.status_code, 428)
        self.assertTrue(invalid_captcha.json()["captcha_required"])
        self.assertEqual(
            self.database.get_faculty_password_failed_attempts(67890, 123456),
            0,
        )

        turnstile_client.post = AsyncMock(side_effect=httpx.ConnectError("offline"))
        with self.assertLogs("quizcord_analytics.dashboard", level="ERROR"):
            with patch(
                "quizcord_bot.web.httpx.AsyncClient",
                return_value=turnstile_context,
            ):
                unavailable_captcha = self.client.post(
                    "/api/faculty/password/verify",
                    json={
                        "password": "Temporary!FacultyPassword",
                        "turnstile_token": "valid-token",
                    },
                )
        self.assertEqual(unavailable_captcha.status_code, 503)

        valid_response = Mock()
        valid_response.json.return_value = {
            "success": True,
            "hostname": "analytics.example.com",
            "action": "faculty_password",
        }
        turnstile_client.post = AsyncMock(return_value=valid_response)
        with patch(
            "quizcord_bot.web.httpx.AsyncClient",
            return_value=turnstile_context,
        ):
            first_failure = self.client.post(
                "/api/faculty/password/verify",
                json={
                    "password": "wrong-password",
                    "turnstile_token": "valid-token",
                },
            )
        self.assertEqual(first_failure.status_code, 401)
        self.assertTrue(first_failure.json()["captcha_required"])
        turnstile_client.post = AsyncMock(return_value=valid_response)
        with patch(
            "quizcord_bot.web.httpx.AsyncClient",
            return_value=turnstile_context,
        ):
            verification = self.client.post(
                "/api/faculty/password/verify",
                json={
                    "password": "Temporary!FacultyPassword",
                    "turnstile_token": "valid-token",
                },
            )
        self.assertEqual(verification.status_code, 200)
        self.assertTrue(verification.json()["must_change"])
        self.assertEqual(
            turnstile_client.post.await_args.args[0],
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        )
        self.assertEqual(
            turnstile_client.post.await_args.kwargs["data"],
            {
                "secret": "turnstile-secret-key",
                "response": "valid-token",
            },
        )
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
        student_page = self.client.get("/faculty/students/4GM22CS001")
        self.assertEqual(student_page.status_code, 200)
        self.assertIn("Quizcord Faculty", student_page.text)
        self.assertIn('href="/faculty">Back to students', student_page.text)
        self.assertIn("/static/faculty-student.js", student_page.text)
        self.assertNotIn('id="faculty-password-link"', student_page.text)
        self.assertNotIn("/admin/faculty-passwords", student_page.text)
        self.assertNotIn('id="dashboard-tabs"', student_page.text)
        self.assertEqual(
            self.client.get("/api/faculty/students/NOTASSIGNED").status_code,
            404,
        )
        self.assertEqual(
            self.client.get("/faculty/students/NOTASSIGNED").status_code,
            404,
        )
        self.assertEqual(self.client.get("/api/sessions").status_code, 403)

    def test_dashboard_redirects_to_sign_in_and_sets_security_headers(self) -> None:
        response = self.client.get("/", follow_redirects=False)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"], "/login")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-frame-options"], "DENY")

    def test_dashboard_static_assets_are_served(self) -> None:
        for path, content_type, expected in (
            (
                "/static/dashboard-theme.css",
                "text/css",
                "::view-transition-new(root)",
            ),
            (
                "/static/dashboard-theme.js",
                "javascript",
                "circle(0px at",
            ),
            (
                "/static/dashboard-theme.js",
                "javascript",
                "document.startViewTransition",
            ),
            (
                "/static/admin-dashboard.css",
                "text/css",
                ".dashboard-tabs",
            ),
            (
                "/static/admin-dashboard.css",
                "text/css",
                ".directory-search-field",
            ),
            (
                "/static/admin-dashboard.css",
                "text/css",
                ".session-search-field",
            ),
            (
                "/static/admin-dashboard.css",
                "text/css",
                "[hidden] { display: none !important; }",
            ),
            (
                "/static/admin-dashboard.css",
                "text/css",
                ".attendance-toggle",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "Students who attended",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "No students match your search.",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "Show not attended (",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "student-page-heading",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "sessionHeading.hidden=true",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "Search names or USN",
            ),
            (
                "/static/admin-dashboard.js",
                "javascript",
                "View analytics",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                "maskAllText: true",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                'replay.stop({ flush: false })',
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                "maxReplayDuration: 900000",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                "startRevision !== consentRevision",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                'tracesSampler: () => (readChoice() === "accepted" ? 0.1 : 0)',
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                "urlQueryParams: false",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                "beforeSendSpan: scrubSpan",
            ),
            (
                "/static/cookie-preferences.js",
                "javascript",
                'if (/url|query/i.test(key))',
            ),
            (
                "/static/faculty-dashboard.css",
                "text/css",
                ".candidate-warning",
            ),
            (
                "/static/faculty-student.js",
                "javascript",
                "/api/faculty/students/",
            ),
            (
                "/static/icons/moon.svg",
                "image/svg+xml",
                "<svg",
            ),
            (
                "/static/icons/users.svg",
                "image/svg+xml",
                "<svg",
            ),
            (
                "/static/faculty-dashboard.js",
                "javascript",
                'guild.is_faculty||guild.role==="admin"',
            ),
            (
                "/static/faculty-dashboard.js",
                "javascript",
                'if(!query){candidates.replaceChildren(el("div","empty","Enter a USN to find students."));return;}',
            ),
            (
                "/static/faculty-dashboard.js",
                "javascript",
                "loadAssigned();}).catch",
            ),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(content_type, response.headers["content-type"])
                self.assertIn(expected, response.text)

    def test_health_endpoint_does_not_require_sign_in(self) -> None:
        response = self.client.get("/health")
        head_response = self.client.head("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(head_response.status_code, 200)
        self.assertEqual(head_response.content, b"")

    def test_sentry_http_metrics_use_the_matched_route_template(self) -> None:
        app = create_dashboard_app(
            replace(
                self.settings,
                sentry_dsn="https://public@example.ingest.sentry.io/123",
            ),
            self.database,
        )
        with patch("quizcord_bot.web.record_http_request") as record_metric:
            with TestClient(app, base_url="https://testserver") as client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        record_metric.assert_called_once()
        method, route, status_code, duration = record_metric.call_args.args
        self.assertEqual(method, "GET")
        self.assertEqual(route, "/health")
        self.assertEqual(status_code, 200)
        self.assertGreaterEqual(duration, 0)

    def test_public_policy_pages_describe_data_and_link_to_each_other(self) -> None:
        privacy = self.client.get("/privacy")
        terms = self.client.get("/terms")

        self.assertEqual(privacy.status_code, 200)
        self.assertIn("Privacy Policy", privacy.text)
        self.assertIn("marked inactive", privacy.text)
        self.assertIn("chandutalawar187@gmail.com", privacy.text)
        self.assertIn('href="/terms"', privacy.text)
        self.assertIn("Sentry monitoring", privacy.text)
        self.assertIn("request bodies", privacy.text)
        self.assertIn("generic severity labels", privacy.text)
        self.assertIn("route templates", privacy.text)
        self.assertIn("Cookies and optional analytics", privacy.text)
        self.assertIn("Cookie settings", privacy.text)
        self.assertIn("quizcord_analytics_consent", privacy.text)
        self.assertIn("All page text and input values are masked", privacy.text)
        self.assertIn(
            "Faculty pages and individual student analytics pages are excluded",
            privacy.text,
        )
        self.assertEqual(terms.status_code, 200)
        self.assertIn("Terms of Service", terms.text)
        self.assertIn('href="/privacy"', terms.text)

    def test_session_replay_html_is_enabled_only_for_admin_dashboard(self) -> None:
        enabled = _render_admin_dashboard(
            "https://public@example.ingest.sentry.io/123", True
        )
        disabled = _render_admin_dashboard(
            "https://public@example.ingest.sentry.io/123", False
        )

        self.assertIn('data-session-replay-enabled="true"', enabled)
        self.assertIn("Cookie preferences", enabled)
        self.assertIn("cookie-analytics-enabled", enabled)
        self.assertIn("Optional analytics and performance", enabled)
        self.assertIn(
            'data-session-replay-dsn="https://public@example.ingest.sentry.io/123"',
            enabled,
        )
        self.assertIn('data-session-replay-enabled="false"', disabled)
        self.assertIn('data-session-replay-dsn=""', disabled)

    def test_sentry_browser_csp_allows_only_configured_ingest_origin(self) -> None:
        settings = replace(
            self.settings,
            sentry_dsn="https://public@example.ingest.sentry.io/123",
        )
        app = create_dashboard_app(settings, self.database)

        with TestClient(app, base_url="https://testserver") as client:
            response = client.get("/health")

        policy = response.headers["Content-Security-Policy"]
        self.assertIn("https://browser.sentry-cdn.com", policy)
        self.assertIn("https://example.ingest.sentry.io", policy)
        self.assertIn("worker-src 'self' blob:", policy)

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

        detail = _summary_detail(
            summary,
            (QuizSessionAbsentStudent("Student Two", "4GM22CS002"),),
        )

        self.assertEqual(detail["participants"][0]["usn"], "ABC123")
        self.assertFalse(detail["participants"][0]["name_matches_usn_format"])
        self.assertNotIn("discord_id", detail["participants"][0])
        self.assertEqual(
            detail["not_attended"],
            [{"display_name": "Student Two", "usn": "4GM22CS002"}],
        )


if __name__ == "__main__":
    unittest.main()
