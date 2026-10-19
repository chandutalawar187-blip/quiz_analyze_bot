"""Authenticated live web dashboard for Quizcord sessions."""

import asyncio
import html
import logging
from pathlib import Path
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from quizcord_bot.config import Settings
from quizcord_bot.database import (
    Database,
    DashboardUser,
    QuizSessionSummary,
    StudentQuizAnalytics,
    matches_usn_format,
)
from quizcord_bot.exports import quiz_session_workbook, student_analytics_workbook
from quizcord_bot.telemetry import record_http_request
from quizcord_bot.templates import (
    FACULTY_ADMIN_PAGE,
    FACULTY_LOGIN_PAGE,
    FACULTY_PAGE,
    FACULTY_PASSWORD_PAGE,
    HTML_PAGE,
    PRIVACY_POLICY_PAGE,
    TERMS_OF_SERVICE_PAGE,
    render_admin_login_page,
    render_error_page,
)


LOGGER = logging.getLogger("quizcord_analytics.dashboard")
DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD_PERMISSION = 1 << 5
ADMINISTRATOR_PERMISSION = 1 << 3
OAUTH_SESSION_TTL_SECONDS = 3600
PERMISSION_CACHE_SECONDS = 60
RATE_LIMIT_MAX_BUCKETS = 10_000
RATE_LIMIT_RULES = {
    "/auth/login": (12, 60),
    "/auth/callback": (20, 60),
    "/api/faculty/password/verify": (10, 900),
    "/api/faculty/password/change": (10, 900),
    "/api/admin/faculty-passwords": (10, 60),
    "/api/faculty/candidates": (30, 60),
}
DEFAULT_REQUEST_RATE_LIMIT = (300, 60)
DEFAULT_API_RATE_LIMIT = (120, 60)


def _error_page(status_code: int, detail: Any) -> str:
    return render_error_page(status_code, detail)


def _wants_json_error(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").casefold()
    return "application/json" in accept and "text/html" not in accept


@dataclass
class _OAuthSession:
    user_id: int
    username: str
    access_token: str
    refresh_token: str
    access_token_expires_at: float
    expires_at: float
    last_permission_check: float
    admin_guild_ids: set[int]
    faculty_guild_ids: set[int]
    faculty_password_verified_guild_ids: set[int]
    must_change_faculty_guild_ids: set[int]
    permission_check_lock: asyncio.Lock


def _dashboard_user(user: DashboardUser) -> dict[str, Any]:
    return {
        "account_name": user.account_name,
        "global_name": user.global_name,
        "server_nickname": user.server_nickname,
        "usn": user.usn,
        "student_name": user.student_name,
        "name_matches_usn_format": user.name_matches_usn_format,
    }


def create_dashboard_app(settings: Settings, database: Database) -> FastAPI:
    app = FastAPI(title="Quizcord Analytics", docs_url=None, redoc_url=None)
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).resolve().parent / "static"),
        name="static",
    )
    if settings.dashboard_session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.dashboard_session_secret,
            session_cookie="quizcord_dashboard",
            max_age=OAUTH_SESSION_TTL_SECONDS,
            same_site="lax",
            https_only=True,
        )
    oauth_sessions: dict[str, _OAuthSession] = {}
    rate_limit_windows: OrderedDict[
        tuple[str, str], deque[float]
    ] = OrderedDict()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        rules: list[tuple[str, int, int]] = []
        if path != "/health":
            rules.append(("all-requests", *DEFAULT_REQUEST_RATE_LIMIT))
        if path.startswith("/api/"):
            rules.append(("api", *DEFAULT_API_RATE_LIMIT))
        specific_limit = RATE_LIMIT_RULES.get(path)
        if specific_limit is None and path.startswith(
            "/api/admin/faculty-passwords/"
        ):
            specific_limit = RATE_LIMIT_RULES["/api/admin/faculty-passwords"]
        if specific_limit is not None:
            bucket_name = (
                "admin-password-provision"
                if path.startswith("/api/admin/")
                else path
            )
            rules.append((bucket_name, *specific_limit))

        if rules:
            client_ip = request.headers.get("x-real-ip", "").strip()
            if not client_ip:
                client_ip = (
                    request.client.host
                    if request.client is not None
                    else "unknown"
                )
            now = time.monotonic()
            buckets: list[tuple[tuple[str, str], deque[float], int, int]] = []
            exceeded: tuple[deque[float], int, int] | None = None
            for bucket_name, limit, window_seconds in rules:
                key = (client_ip, bucket_name)
                attempts = rate_limit_windows.get(key)
                if attempts is None:
                    attempts = deque()
                else:
                    rate_limit_windows.move_to_end(key)
                cutoff = now - window_seconds
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                buckets.append((key, attempts, limit, window_seconds))
                if exceeded is None and len(attempts) >= limit:
                    exceeded = (attempts, limit, window_seconds)

            if exceeded is not None:
                attempts, limit, window_seconds = exceeded
                retry_after = max(
                    1, int(attempts[0] + window_seconds - now + 0.999)
                )
                headers = {
                    "Retry-After": str(retry_after),
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(int(time.time()) + retry_after),
                }
                if path.startswith("/api/"):
                    response: Response = JSONResponse(
                        {
                            "detail": (
                                "Too many requests. Please wait before trying again."
                            )
                        },
                        status_code=429,
                        headers=headers,
                    )
                else:
                    headers["Content-Type"] = "text/html; charset=utf-8"
                    response = HTMLResponse(
                        _error_page(
                            429,
                            "Please wait before trying this sign-in action again.",
                        ),
                        status_code=429,
                        headers=headers,
                    )
            else:
                header_bucket = min(
                    buckets,
                    key=lambda bucket: bucket[2] / bucket[3],
                )
                for key, attempts, _, _ in buckets:
                    if key not in rate_limit_windows:
                        if len(rate_limit_windows) >= RATE_LIMIT_MAX_BUCKETS:
                            rate_limit_windows.popitem(last=False)
                        rate_limit_windows[key] = attempts
                    attempts.append(now)
                response = await call_next(request)
                key, attempts, header_limit, header_window = header_bucket
                reset_after = max(
                    1, int(attempts[0] + header_window - now + 0.999)
                )
                response.headers["X-RateLimit-Limit"] = str(header_limit)
                response.headers["X-RateLimit-Remaining"] = str(
                    max(0, header_limit - len(attempts))
                )
                response.headers["X-RateLimit-Reset"] = str(
                    int(time.time()) + reset_after
                )
        else:
            response = await call_next(request)

        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.middleware("http")
    async def collect_http_metrics(request: Request, call_next):
        if settings.sentry_dsn is None:
            return await call_next(request)
        started_at = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            record_http_request(
                request.method,
                route_template,
                status_code,
                time.perf_counter() - started_at,
            )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, error: StarletteHTTPException
    ) -> Response:
        if _wants_json_error(request):
            return JSONResponse(
                {"detail": error.detail},
                status_code=error.status_code,
                headers=error.headers,
            )
        return HTMLResponse(
            _error_page(error.status_code, error.detail),
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request, error: Exception
    ) -> Response:
        LOGGER.exception("Unhandled dashboard request failure.")
        detail = "An unexpected server error occurred. Please try again."
        if _wants_json_error(request):
            return JSONResponse({"detail": detail}, status_code=500)
        return HTMLResponse(_error_page(500, detail), status_code=500)

    @app.get("/health")
    @app.head("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy_policy() -> HTMLResponse:
        return HTMLResponse(PRIVACY_POLICY_PAGE)

    @app.get("/terms", response_class=HTMLResponse)
    async def terms_of_service() -> HTMLResponse:
        return HTMLResponse(TERMS_OF_SERVICE_PAGE)

    def require_oauth_config() -> tuple[str, str, str]:
        if (
            settings.dashboard_public_url is None
            or settings.discord_oauth_client_id is None
            or settings.discord_oauth_client_secret is None
        ):
            raise HTTPException(
                status_code=503,
                detail="The web dashboard is not configured.",
            )
        return (
            settings.dashboard_public_url,
            settings.discord_oauth_client_id,
            settings.discord_oauth_client_secret,
        )

    async def current_authenticated(request: Request) -> _OAuthSession:
        require_oauth_config()
        session_key = request.session.get("dashboard_session")
        auth_session = oauth_sessions.get(session_key) if session_key else None
        if auth_session is None or auth_session.expires_at <= time.time():
            request.session.clear()
            raise HTTPException(status_code=401, detail="Please sign in again.")

        async with auth_session.permission_check_lock:
            now = time.time()
            if now - auth_session.last_permission_check >= PERMISSION_CACHE_SECONDS:
                auth_session = await _verify_current_guild_access(
                    auth_session, settings, database
                )
                oauth_sessions[session_key] = auth_session
        if not auth_session.admin_guild_ids and not auth_session.faculty_guild_ids:
            raise HTTPException(
                status_code=403,
                detail="Administrator or Faculty role access to an installed server is required.",
            )
        return auth_session

    def resolve_guild(request: Request, auth_session: _OAuthSession) -> int:
        installed = dict(database.get_installed_guilds())
        available = (
            auth_session.admin_guild_ids | auth_session.faculty_guild_ids
        ) & installed.keys()
        requested = request.query_params.get("guild_id")
        if requested is None:
            requested = request.session.get("selected_guild_id")
        if requested is not None:
            try:
                guild_id = int(requested)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422, detail="Invalid server selection."
                ) from None
            if guild_id not in available:
                raise HTTPException(
                    status_code=403,
                    detail="You do not have access to that installed server.",
                )
        else:
            ordered = sorted(
                available,
                key=lambda item: (
                    item not in auth_session.admin_guild_ids,
                    item,
                ),
            )
            if not ordered:
                raise HTTPException(
                    status_code=403,
                    detail="No accessible installed servers were found.",
                )
            guild_id = ordered[0]
        request.session["selected_guild_id"] = str(guild_id)
        request.state.guild_id = guild_id
        return guild_id

    async def current_admin(request: Request) -> _OAuthSession:
        auth_session = await current_authenticated(request)
        guild_id = resolve_guild(request, auth_session)
        if guild_id not in auth_session.admin_guild_ids:
            raise HTTPException(
                status_code=403,
                detail="Manage Server permission is required for this server.",
            )
        return auth_session

    async def current_faculty(
        request: Request, require_password: bool = True
    ) -> _OAuthSession:
        auth_session = await current_authenticated(request)
        guild_id = resolve_guild(request, auth_session)
        if (
            guild_id not in auth_session.faculty_guild_ids
            and guild_id not in auth_session.admin_guild_ids
        ):
            raise HTTPException(
                status_code=403,
                detail="The configured Faculty role is required in this server.",
            )
        if require_password and guild_id in auth_session.faculty_guild_ids and (
            guild_id not in auth_session.faculty_password_verified_guild_ids
            or guild_id in auth_session.must_change_faculty_guild_ids
        ):
            raise HTTPException(
                status_code=428,
                detail="Complete faculty password verification before continuing.",
            )
        return auth_session

    @app.get("/auth/login")
    async def login(
        request: Request,
        session: str | None = None,
        guild_id: str | None = None,
        destination: str | None = None,
    ) -> RedirectResponse:
        public_url, client_id, _ = require_oauth_config()
        now = time.time()
        expired_sessions = [
            key
            for key, auth_session in oauth_sessions.items()
            if auth_session.expires_at <= now
        ]
        for key in expired_sessions:
            del oauth_sessions[key]
        state = secrets.token_urlsafe(32)
        request.session.clear()
        request.session["oauth_state"] = state
        if destination == "faculty":
            request.session["login_destination"] = "faculty"
            request.session["return_to"] = "/faculty/password"
        else:
            query: dict[str, str] = {}
            if session and len(session) == 32 and all(
                character in "0123456789abcdef"
                for character in session.casefold()
            ):
                query["session"] = session
            if guild_id and guild_id.isdecimal() and int(guild_id) > 0:
                query["guild_id"] = guild_id
                request.session["requested_guild_id"] = guild_id
            if query:
                request.session["return_to"] = f"/?{urlencode(query)}"
        params = {
            "client_id": client_id,
            "redirect_uri": f"{public_url}/auth/callback",
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
        }
        return RedirectResponse(
            f"https://discord.com/oauth2/authorize?{urlencode(params)}",
            status_code=302,
        )

    @app.get("/auth/callback")
    async def callback(
        request: Request, code: str | None = None, state: str | None = None
    ) -> RedirectResponse:
        public_url, client_id, client_secret = require_oauth_config()
        expected_state = request.session.pop("oauth_state", None)
        if (
            not code
            or not state
            or not expected_state
            or not secrets.compare_digest(state, expected_state)
        ):
            request.session.clear()
            raise HTTPException(status_code=400, detail="Invalid sign-in state.")

        redirect_uri = f"{public_url}/auth/callback"
        oauth_stage = "authorization-code exchange"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                token_response = await client.post(
                    f"{DISCORD_API}/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                token_response.raise_for_status()
                token = token_response.json()
                access_token = token["access_token"]
                oauth_stage = "Discord identity lookup"
                user_response = await client.get(
                    f"{DISCORD_API}/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                user_response.raise_for_status()
                user = user_response.json()
                oauth_stage = "Discord server-membership lookup"
                guilds_response = await client.get(
                    f"{DISCORD_API}/users/@me/guilds",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                guilds_response.raise_for_status()
                guilds = guilds_response.json()
                installed_guilds = database.get_installed_guilds()
                admin_guild_ids = {
                    guild_id
                    for guild_id, _ in installed_guilds
                    if _is_guild_admin(guilds, guild_id)
                }
                faculty_guild_ids: set[int] = set()
                for guild_id, _ in installed_guilds:
                    if not _is_guild_member(guilds, guild_id):
                        continue
                    faculty_role_id = database.get_faculty_role_id(guild_id)
                    if faculty_role_id is None:
                        continue
                    oauth_stage = "Faculty role lookup"
                    member = await _get_guild_member(
                        client, settings, int(user["id"]), guild_id
                    )
                    if _is_guild_faculty(member, faculty_role_id):
                        faculty_guild_ids.add(guild_id)
        except (httpx.HTTPError, KeyError, ValueError) as error:
            status_code = (
                error.response.status_code
                if isinstance(error, httpx.HTTPStatusError)
                else None
            )
            LOGGER.exception(
                "Discord OAuth failed during %s (HTTP %s).",
                oauth_stage,
                status_code or "transport/response parsing",
            )
            raise HTTPException(
                status_code=502,
                detail=_oauth_failure_detail(oauth_stage, status_code),
            ) from None

        if not admin_guild_ids and not faculty_guild_ids:
            await _revoke_oauth_token(
                access_token, client_id, client_secret
            )
            raise HTTPException(
                status_code=403,
                detail="This Discord account does not have access to an installed server.",
            )
        return_to = request.session.get("return_to", "/")
        login_destination = request.session.get("login_destination")
        requested_guild_id = request.session.get("requested_guild_id")
        session_key = secrets.token_urlsafe(32)
        now = time.time()
        oauth_sessions[session_key] = _OAuthSession(
            user_id=int(user["id"]),
            username=str(user.get("global_name") or user.get("username") or "Admin"),
            access_token=access_token,
            refresh_token=str(token.get("refresh_token") or ""),
            access_token_expires_at=now + int(token.get("expires_in", 3600)),
            expires_at=now + OAUTH_SESSION_TTL_SECONDS,
            last_permission_check=now,
            admin_guild_ids=admin_guild_ids,
            faculty_guild_ids=faculty_guild_ids,
            faculty_password_verified_guild_ids=set(),
            must_change_faculty_guild_ids=set(),
            permission_check_lock=asyncio.Lock(),
        )
        request.session.clear()
        request.session["dashboard_session"] = session_key
        preferred_guild_ids = (
            faculty_guild_ids | admin_guild_ids
            if login_destination == "faculty"
            else admin_guild_ids | faculty_guild_ids
        )
        selected_guild_id = None
        if isinstance(requested_guild_id, str) and requested_guild_id.isdecimal():
            candidate_guild_id = int(requested_guild_id)
            if candidate_guild_id in preferred_guild_ids:
                selected_guild_id = candidate_guild_id
        if selected_guild_id is None:
            selected_guild_id = next(
                (
                    guild_id
                    for guild_id, _ in installed_guilds
                    if guild_id in preferred_guild_ids
                    and (
                        login_destination != "faculty"
                        or guild_id in faculty_guild_ids
                        or not faculty_guild_ids
                    )
                ),
                None,
            )
        if selected_guild_id is not None:
            request.session["selected_guild_id"] = str(selected_guild_id)
        return RedirectResponse(return_to, status_code=302)

    @app.get("/auth/logout")
    async def logout(request: Request) -> RedirectResponse:
        _, client_id, client_secret = require_oauth_config()
        session_key = request.session.pop("dashboard_session", None)
        if session_key:
            auth_session = oauth_sessions.pop(session_key, None)
            if auth_session:
                await _revoke_oauth_token(
                    auth_session.access_token, client_id, client_secret
                )
        request.session.clear()
        return RedirectResponse("/", status_code=302)

    @app.get("/api/guilds")
    async def available_guilds(request: Request) -> list[dict[str, str]]:
        auth_session = await current_authenticated(request)
        installed = dict(database.get_installed_guilds())
        guild_ids = auth_session.admin_guild_ids | auth_session.faculty_guild_ids
        return [
            {
                "id": str(guild_id),
                "name": name,
                "role": (
                    "admin"
                    if guild_id in auth_session.admin_guild_ids
                    else "faculty"
                ),
            }
            for guild_id, name in sorted(
                installed.items(), key=lambda item: item[1].casefold()
            )
            if guild_id in guild_ids
        ]

    @app.post("/api/guilds/select")
    async def select_guild(
        request: Request, payload: dict[str, str] = Body(...)
    ) -> dict[str, str]:
        auth_session = await current_authenticated(request)
        try:
            guild_id = int(payload.get("guild_id", ""))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="Invalid server selection."
            ) from None
        available = (
            auth_session.admin_guild_ids | auth_session.faculty_guild_ids
        )
        guilds = dict(database.get_installed_guilds())
        if guild_id not in available or guild_id not in guilds:
            raise HTTPException(
                status_code=403,
                detail="You do not have access to that installed server.",
            )
        request.session["selected_guild_id"] = str(guild_id)
        return {"guild_id": str(guild_id), "name": guilds[guild_id]}

    @app.get("/", response_class=HTMLResponse, response_model=None)
    async def index(request: Request) -> HTMLResponse | RedirectResponse:
        require_oauth_config()
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                login_params: dict[str, str] = {}
                requested_session = request.query_params.get("session", "")
                requested_guild = request.query_params.get("guild_id", "")
                if len(requested_session) == 32 and all(
                    character in "0123456789abcdef"
                    for character in requested_session.casefold()
                ):
                    login_params["session"] = requested_session
                if requested_guild.isdecimal() and int(requested_guild) > 0:
                    login_params["guild_id"] = requested_guild
                suffix = f"?{urlencode(login_params)}" if login_params else ""
                return RedirectResponse(f"/login{suffix}", status_code=302)
            raise
        return HTMLResponse(HTML_PAGE)

    @app.get("/faculty", response_class=HTMLResponse, response_model=None)
    async def faculty_page(request: Request) -> HTMLResponse | RedirectResponse:
        require_oauth_config()
        try:
            await current_faculty(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/faculty/login", status_code=302)
            if error.status_code == 428:
                return RedirectResponse("/faculty/password", status_code=302)
            raise
        return HTMLResponse(FACULTY_PAGE)

    @app.get("/faculty/login", response_class=HTMLResponse)
    async def faculty_login_page() -> HTMLResponse:
        require_oauth_config()
        return HTMLResponse(FACULTY_LOGIN_PAGE)

    @app.get(
        "/faculty/password",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def faculty_password_page(request: Request) -> HTMLResponse | RedirectResponse:
        try:
            auth_session = await current_authenticated(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/faculty/login", status_code=302)
            raise
        guild_id = resolve_guild(request, auth_session)
        if (
            guild_id not in auth_session.faculty_guild_ids
            and guild_id in auth_session.admin_guild_ids
        ):
            return RedirectResponse("/faculty", status_code=302)
        if guild_id not in auth_session.faculty_guild_ids:
            raise HTTPException(
                status_code=403,
                detail="The configured Faculty role is required in this server.",
            )
        page = FACULTY_PASSWORD_PAGE.replace(
            "__TURNSTILE_SITE_KEY__",
            html.escape(settings.turnstile_site_key or "", quote=True),
        )
        return HTMLResponse(page)

    @app.get(
        "/admin/faculty-passwords",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def faculty_password_admin_page(
        request: Request,
    ) -> HTMLResponse | RedirectResponse:
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/login", status_code=302)
            raise
        return HTMLResponse(FACULTY_ADMIN_PAGE)

    @app.get(
        "/students/{usn}",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def student_page(
        usn: str, request: Request
    ) -> HTMLResponse | RedirectResponse:
        try:
            await current_admin(request)
        except HTTPException as error:
            if error.status_code == 401:
                return RedirectResponse("/login", status_code=302)
            raise
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        return HTMLResponse(HTML_PAGE)

    @app.get("/faculty/students/{usn}", response_class=HTMLResponse)
    async def faculty_student_page(usn: str, request: Request) -> HTMLResponse:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        if usn.casefold() not in {
            assigned.casefold()
            for assigned in database.get_faculty_student_usns(
                request.state.guild_id, auth_session.user_id
            )
        }:
            raise HTTPException(status_code=404, detail="Student not found.")
        return HTMLResponse(HTML_PAGE)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(
        session: str | None = None,
        guild_id: str | None = None,
        destination: str | None = None,
    ) -> HTMLResponse:
        require_oauth_config()
        query = "?destination=faculty" if destination == "faculty" else ""
        if not query and session and len(session) == 32 and all(
            character in "0123456789abcdef" for character in session.casefold()
        ):
            query = f"?session={session}"
        if guild_id and guild_id.isdecimal() and int(guild_id) > 0:
            query = (
                f"?{urlencode({'session': session, 'guild_id': guild_id})}"
                if session
                else f"?guild_id={guild_id}"
            )
        return HTMLResponse(render_admin_login_page(query))

    @app.get("/api/me")
    async def me(request: Request) -> dict[str, str]:
        auth_session = await current_authenticated(request)
        guild_id = resolve_guild(request, auth_session)
        return {
            "id": str(auth_session.user_id),
            "username": auth_session.username,
            "guild_id": str(guild_id),
            "role": "admin" if guild_id in auth_session.admin_guild_ids else "faculty",
        }

    @app.get("/api/sessions")
    async def sessions(request: Request) -> list[dict[str, Any]]:
        await current_admin(request)
        return [
            _summary_overview(summary)
            for summary in database.get_quiz_session_summaries(
                request.state.guild_id, limit=50
            )
        ]

    @app.get("/api/users")
    async def users(request: Request) -> list[dict[str, Any]]:
        await current_admin(request)
        return [
            _dashboard_user(user)
            for user in database.get_dashboard_users(request.state.guild_id)
        ]

    @app.get("/api/faculty/students")
    async def faculty_students(request: Request) -> list[dict[str, Any]]:
        auth_session = await current_faculty(request)
        assigned_usns = database.get_faculty_student_usns(
            request.state.guild_id, auth_session.user_id
        )
        dashboard_users = {
            user.usn.casefold(): user
            for user in database.get_dashboard_users(request.state.guild_id)
            if user.usn is not None
        }
        students = []
        for usn in assigned_usns:
            user = dashboard_users.get(usn.casefold())
            analytics = database.get_student_quiz_analytics(
                request.state.guild_id, usn
            )
            students.append(
                {
                    "usn": usn,
                    "student_name": (
                        (
                            user.student_name
                            or user.global_name
                            or user.server_nickname
                        )
                        if user
                        else analytics.student_name
                    ),
                    "quizzes_attended": analytics.quizzes_attended,
                    "completed_quizzes": analytics.completed_quizzes,
                    "attendance_percentage": analytics.attendance_percentage,
                    "total_points": analytics.total_points,
                }
            )
        return students

    @app.post("/api/faculty/password/verify", response_model=None)
    async def verify_faculty_password(
        request: Request, payload: dict[str, str] = Body(...)
    ) -> dict[str, bool] | JSONResponse:
        auth_session = await current_faculty(request, require_password=False)
        guild_id = request.state.guild_id
        if guild_id not in auth_session.faculty_guild_ids:
            raise HTTPException(
                status_code=403, detail="The configured Faculty role is required."
            )
        if not settings.turnstile_secret_key:
            raise HTTPException(
                status_code=503,
                detail="Security check is not configured. Contact an administrator.",
            )
        token = payload.get("turnstile_token", "")
        if not token or len(token) > 2048:
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Complete the security check before continuing.",
                    "captcha_required": True,
                },
            )
        if not await _verify_turnstile_token(settings, token):
            return JSONResponse(
                status_code=428,
                content={
                    "detail": "Security check expired or invalid. Please retry it.",
                    "captcha_required": True,
                },
            )
        password = payload.get("password", "")
        if not password or len(password) > 256:
            raise HTTPException(status_code=422, detail="Enter your faculty password.")
        valid, must_change = database.verify_faculty_password(
            guild_id, auth_session.user_id, password
        )
        if not valid:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Faculty password is incorrect, not provisioned, or temporarily locked. Contact an administrator if you need a reset.",
                    "captcha_required": True,
                },
            )
        auth_session.faculty_password_verified_guild_ids.add(guild_id)
        if must_change:
            auth_session.must_change_faculty_guild_ids.add(guild_id)
        else:
            auth_session.must_change_faculty_guild_ids.discard(guild_id)
        return {"must_change": must_change}

    @app.post("/api/faculty/password/change")
    async def change_faculty_password(
        request: Request, payload: dict[str, str] = Body(...)
    ) -> dict[str, bool]:
        auth_session = await current_faculty(request, require_password=False)
        guild_id = request.state.guild_id
        if guild_id not in auth_session.faculty_guild_ids:
            raise HTTPException(
                status_code=403, detail="The configured Faculty role is required."
            )
        current_password = payload.get("current_password", "")
        new_password = payload.get("new_password", "")
        if len(new_password) < 12 or len(new_password) > 128:
            raise HTTPException(
                status_code=422,
                detail="Your new faculty password must be between 12 and 128 characters.",
            )
        valid, _ = database.verify_faculty_password(
            guild_id, auth_session.user_id, current_password
        )
        if not valid:
            raise HTTPException(
                status_code=401, detail="The current faculty password is incorrect."
            )
        if secrets.compare_digest(current_password, new_password):
            raise HTTPException(
                status_code=422,
                detail="Choose a new password different from the temporary password.",
            )
        database.change_faculty_password(
            guild_id, auth_session.user_id, new_password
        )
        auth_session.faculty_password_verified_guild_ids.add(guild_id)
        auth_session.must_change_faculty_guild_ids.discard(guild_id)
        return {"changed": True}

    @app.post("/api/admin/faculty-passwords/{discord_id}")
    async def provision_faculty_password(
        discord_id: int, request: Request
    ) -> dict[str, str]:
        await current_admin(request)
        guild_id = request.state.guild_id
        faculty_role_id = database.get_faculty_role_id(guild_id)
        if faculty_role_id is None:
            raise HTTPException(
                status_code=503,
                detail="Set this server's faculty role before provisioning passwords with /set_faculty_role.",
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                member = await _get_guild_member(
                    client, settings, discord_id, guild_id
                )
        except httpx.HTTPError:
            LOGGER.exception("Could not verify faculty during password provisioning.")
            raise HTTPException(
                status_code=503,
                detail="Could not verify this Discord member. Please retry.",
            ) from None
        if not _is_guild_faculty(member, faculty_role_id):
            raise HTTPException(
                status_code=404,
                detail="No current member with the configured Faculty role was found for that Discord ID.",
            )
        temporary_password = secrets.token_urlsafe(18)
        database.set_faculty_temporary_password(
            guild_id, discord_id, temporary_password
        )
        return {
            "discord_id": str(discord_id),
            "temporary_password": temporary_password,
        }

    @app.get("/api/faculty/candidates")
    async def faculty_candidates(
        request: Request, search: str = ""
    ) -> list[dict[str, Any]]:
        auth_session = await current_faculty(request)
        query = search.strip().upper()
        if len(query) > 100:
            raise HTTPException(status_code=422, detail="USN search is too long.")
        assigned_usns = {
            usn.casefold()
            for usn in database.get_faculty_student_usns(
                request.state.guild_id, auth_session.user_id
            )
        }
        candidates = []
        for user in database.get_dashboard_users(request.state.guild_id):
            if query and (
                user.usn is None or query not in user.usn.upper()
            ):
                continue
            candidates.append(
                {
                    "usn": user.usn,
                    "student_name": (
                        user.student_name
                        or user.global_name
                        or user.server_nickname
                        or user.usn
                        or user.account_name
                    ),
                    "can_assign": (
                        user.usn is not None
                        and matches_usn_format(user.global_name or "")
                        and user.usn.casefold()
                        == (user.global_name or "").strip().casefold()
                    ),
                    "name_matches_usn_format": user.name_matches_usn_format,
                    "assigned": (
                        user.usn.casefold() in assigned_usns
                        if user.usn is not None
                        else False
                    ),
                }
            )
        return candidates

    @app.post("/api/faculty/students/{usn}", status_code=204)
    async def add_faculty_student(
        usn: str, request: Request
    ) -> Response:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        matching_user = next(
            (
                user
                for user in database.get_dashboard_users(request.state.guild_id)
                if user.usn is not None
                and user.usn.casefold() == usn.casefold()
            ),
            None,
        )
        if matching_user is None:
            raise HTTPException(
                status_code=404,
                detail="No current server member has this USN.",
            )
        if (
            not matches_usn_format(matching_user.global_name or "")
            or usn.casefold()
            != (matching_user.global_name or "").strip().casefold()
        ):
            raise HTTPException(
                status_code=422,
                detail="This student cannot be added until their Discord global display name matches their USN format. Ask them to update it and try again.",
            )
        added = database.add_faculty_student(
            request.state.guild_id, auth_session.user_id, usn
        )
        if not added:
            raise HTTPException(
                status_code=409, detail="This student is already in your list."
            )
        return Response(status_code=204)

    @app.delete("/api/faculty/students/{usn}", status_code=204)
    async def remove_faculty_student(
        usn: str, request: Request
    ) -> Response:
        auth_session = await current_faculty(request)
        if not database.remove_faculty_student(
            request.state.guild_id, auth_session.user_id, usn
        ):
            raise HTTPException(status_code=404, detail="Student not found.")
        return Response(status_code=204)

    @app.get("/api/faculty/students/{usn}")
    async def faculty_student_analytics(
        usn: str, request: Request
    ) -> dict[str, Any]:
        auth_session = await current_faculty(request)
        if not usn.strip() or len(usn) > 100 or usn.casefold() not in {
            assigned.casefold()
            for assigned in database.get_faculty_student_usns(
                request.state.guild_id, auth_session.user_id
            )
        }:
            raise HTTPException(status_code=404, detail="Student not found.")
        return _student_analytics(
            database.get_student_quiz_analytics(request.state.guild_id, usn)
        )

    @app.get("/api/students/{usn}")
    async def student_analytics(usn: str, request: Request) -> dict[str, Any]:
        await current_admin(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        return _student_analytics(
            database.get_student_quiz_analytics(request.state.guild_id, usn)
        )

    @app.get("/api/students/{usn}/export.xlsx")
    async def export_student_analytics(usn: str, request: Request) -> Response:
        await current_admin(request)
        if not usn.strip() or len(usn) > 100:
            raise HTTPException(status_code=404, detail="Student not found.")
        analytics = database.get_student_quiz_analytics(
            request.state.guild_id, usn
        )
        safe_usn = "".join(
            character
            for character in analytics.usn
            if character.isascii() and (character.isalnum() or character in "-_")
        )[:80]
        return Response(
            content=student_analytics_workbook(analytics),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="quizcord-student-{safe_usn or "report"}.xlsx"'
                )
            },
        )

    @app.get("/api/sessions/{session_id}")
    async def session_detail(
        session_id: str, request: Request
    ) -> dict[str, Any]:
        await current_admin(request)
        summary = database.get_quiz_session(session_id)
        if summary is None or summary.guild_id != request.state.guild_id:
            raise HTTPException(status_code=404, detail="Quiz session not found.")
        return _summary_detail(database.get_quiz_session_summary(session_id))

    @app.get("/api/sessions/{session_id}/export.xlsx")
    async def export_session(session_id: str, request: Request) -> Response:
        await current_admin(request)
        session = database.get_quiz_session(session_id)
        if session is None or session.guild_id != request.state.guild_id:
            raise HTTPException(status_code=404, detail="Quiz session not found.")
        summary = database.get_quiz_session_summary(session_id)
        if summary.session.status != "completed":
            raise HTTPException(
                status_code=409,
                detail="The quiz export is available after the quiz completes.",
            )
        quiz_id = summary.session.quizcord_quiz_id or summary.session.session_id
        safe_quiz_id = "".join(
            character for character in quiz_id if character.isalnum() or character in "-_"
        )[:80]
        return Response(
            content=quiz_session_workbook(summary),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="quizcord-{safe_quiz_id}.xlsx"'
            },
        )

    return app


async def _verify_current_guild_access(
    auth_session: _OAuthSession, settings: Settings, database: Database
) -> _OAuthSession:
    now = time.time()
    if auth_session.access_token_expires_at <= now:
        if not auth_session.refresh_token:
            raise HTTPException(status_code=401, detail="Please sign in again.")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    f"{DISCORD_API}/oauth2/token",
                    data={
                        "client_id": settings.discord_oauth_client_id,
                        "client_secret": settings.discord_oauth_client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": auth_session.refresh_token,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                token = response.json()
        except (httpx.HTTPError, ValueError, KeyError):
            LOGGER.exception("Could not refresh a dashboard OAuth session.")
            raise HTTPException(
                status_code=401, detail="Please sign in again."
            ) from None
        auth_session.access_token = token["access_token"]
        auth_session.refresh_token = str(
            token.get("refresh_token") or auth_session.refresh_token
        )
        auth_session.access_token_expires_at = now + int(
            token.get("expires_in", 3600)
        )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(
                f"{DISCORD_API}/users/@me/guilds",
                headers={"Authorization": f"Bearer {auth_session.access_token}"},
            )
            response.raise_for_status()
            guilds = response.json()
            installed_guilds = database.get_installed_guilds()
            auth_session.admin_guild_ids = {
                guild_id
                for guild_id, _ in installed_guilds
                if _is_guild_admin(guilds, guild_id)
            }
            auth_session.faculty_guild_ids.clear()
            for guild_id, _ in installed_guilds:
                if not _is_guild_member(guilds, guild_id):
                    continue
                faculty_role_id = database.get_faculty_role_id(guild_id)
                if faculty_role_id is None:
                    continue
                member = await _get_guild_member(
                    client, settings, auth_session.user_id, guild_id
                )
                if _is_guild_faculty(member, faculty_role_id):
                    auth_session.faculty_guild_ids.add(guild_id)
    except (httpx.HTTPError, ValueError):
        LOGGER.exception("Could not verify dashboard server roles.")
        raise HTTPException(
            status_code=503,
            detail="Dashboard server access could not be verified. Please retry.",
        ) from None

    auth_session.faculty_password_verified_guild_ids.intersection_update(
        auth_session.faculty_guild_ids
    )
    auth_session.must_change_faculty_guild_ids.intersection_update(
        auth_session.faculty_guild_ids
    )
    auth_session.last_permission_check = now
    return auth_session


def _is_guild_admin(guilds: Any, guild_id: int) -> bool:
    if not isinstance(guilds, list):
        return False
    for guild in guilds:
        if not isinstance(guild, dict) or str(guild.get("id")) != str(guild_id):
            continue
        if guild.get("owner") is True:
            return True
        try:
            permissions = int(guild.get("permissions", "0"))
        except (TypeError, ValueError):
            return False
        return bool(
            permissions & (MANAGE_GUILD_PERMISSION | ADMINISTRATOR_PERMISSION)
        )
    return False


def _is_guild_faculty(member: Any, faculty_role_id: int | None) -> bool:
    if faculty_role_id is None or not isinstance(member, dict):
        return False
    roles = member.get("roles")
    if not isinstance(roles, list):
        return False
    return str(faculty_role_id) in {str(role_id) for role_id in roles}


async def _verify_turnstile_token(settings: Settings, token: str) -> bool:
    hostname = urlsplit(settings.dashboard_public_url or "").hostname
    if not settings.turnstile_secret_key or not hostname:
        raise HTTPException(
            status_code=503,
            detail="Security check is not configured. Contact an administrator.",
        )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": settings.turnstile_secret_key,
                    "response": token,
                },
            )
            response.raise_for_status()
            result = response.json()
    except (httpx.HTTPError, ValueError):
        LOGGER.error("Cloudflare Turnstile verification request failed.")
        raise HTTPException(
            status_code=503,
            detail="Security check is temporarily unavailable. Please try again.",
        ) from None
    return (
        isinstance(result, dict)
        and result.get("success") is True
        and result.get("hostname") == hostname
        and result.get("action") == "faculty_password"
    )


async def _get_guild_member(
    client: httpx.AsyncClient,
    settings: Settings,
    user_id: int,
    guild_id: int,
) -> dict[str, Any] | None:
    response = await client.get(
        f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}",
        headers={"Authorization": f"Bot {settings.token}"},
    )
    if response.status_code == 404:
        return None
    if response.status_code == 401:
        LOGGER.error("Discord rejected the bot token during faculty role lookup.")
        raise HTTPException(
            status_code=503,
            detail="Faculty role verification failed because Discord rejected the bot token. Check DISCORD_BOT_TOKEN in the service configuration.",
        )
    if response.status_code == 403:
        LOGGER.error("The bot cannot access the configured server member endpoint.")
        raise HTTPException(
            status_code=503,
            detail="Faculty role verification failed because the bot cannot access this server. Confirm the bot is still a member of that server.",
        )
    response.raise_for_status()
    member = response.json()
    if not isinstance(member, dict):
        raise ValueError("Discord returned an invalid guild member response.")
    return member


def _is_guild_member(guilds: Any, guild_id: int) -> bool:
    return isinstance(guilds, list) and any(
        isinstance(guild, dict) and str(guild.get("id")) == str(guild_id)
        for guild in guilds
    )


def _oauth_failure_detail(stage: str, status_code: int | None) -> str:
    status = f" (HTTP {status_code})" if status_code is not None else ""
    if stage == "authorization-code exchange":
        return (
            "Discord rejected the OAuth authorization code"
            f"{status}. Check that DASHBOARD_PUBLIC_URL matches the Discord "
            "OAuth redirect URL exactly, including /auth/callback, and that "
            "the OAuth client ID and secret belong to that same Discord application. "
            "Restart sign-in to use a fresh authorization code."
        )
    if stage == "Faculty role lookup":
        return (
            "Discord could not verify the Faculty role"
            f"{status}. Confirm the bot can access the configured server and "
            "try signing in again."
        )
    return (
        f"Discord sign-in failed during {stage}{status}. "
        "Check the application logs and try again."
    )


async def _revoke_oauth_token(
    access_token: str, client_id: str, client_secret: str
) -> None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(
                f"{DISCORD_API}/oauth2/token/revoke",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "token": access_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
    except httpx.HTTPError:
        LOGGER.warning("Could not revoke a dashboard OAuth token.")


def _summary_overview(summary: QuizSessionSummary) -> dict[str, Any]:
    session = summary.session
    return {
        "session_id": session.session_id,
        "status": session.status,
        "started_at": session.started_at,
        "completed_at": session.completed_at,
        "quizcord_quiz_id": session.quizcord_quiz_id,
        "question_count": summary.question_count,
        "participant_count": len(summary.participants),
        "attendance_count": summary.attendance_count,
        "eligible_student_count": session.eligible_student_count,
        "attendance_percentage": summary.attendance_percentage,
        "total_points": summary.total_points,
        "average_points": summary.average_points,
    }


def _summary_detail(summary: QuizSessionSummary) -> dict[str, Any]:
    session = summary.session
    return {
        "session": {
            "session_id": session.session_id,
            "status": session.status,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "quizcord_quiz_id": session.quizcord_quiz_id,
        },
        "question_count": summary.question_count,
        "attendance_count": summary.attendance_count,
        "eligible_student_count": session.eligible_student_count,
        "attendance_percentage": summary.attendance_percentage,
        "total_points": summary.total_points,
        "average_points": summary.average_points,
        "participants": [
            {
                "usn": participant.usn,
                "rank": participant.rank,
                "points": participant.points,
                "name_matches_usn_format": participant.name_matches_usn_format,
            }
            for participant in summary.participants
        ],
    }


def _student_analytics(analytics: StudentQuizAnalytics) -> dict[str, Any]:
    return {
        "usn": analytics.usn,
        "student_name": analytics.student_name,
        "quizzes_attended": analytics.quizzes_attended,
        "completed_quizzes": analytics.completed_quizzes,
        "attendance_percentage": analytics.attendance_percentage,
        "total_points": analytics.total_points,
        "average_points": analytics.average_points,
        "history": [
            {
                "quizcord_quiz_id": record.quizcord_quiz_id,
                "status": record.status,
                "started_at": record.started_at,
                "rank": record.rank,
                "points": record.points,
            }
            for record in analytics.history
        ],
    }
