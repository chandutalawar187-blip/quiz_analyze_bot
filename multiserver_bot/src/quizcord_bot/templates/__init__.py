"""Load packaged HTML pages for the dashboard application."""

import html
from importlib.resources import files
from typing import Any

_TEMPLATE_ROOT = files("quizcord_bot").joinpath("templates")


def _load(name: str) -> str:
    return _TEMPLATE_ROOT.joinpath(name).read_text(encoding="utf-8")


HTML_PAGE = _load("dashboard.html")
FACULTY_LOGIN_PAGE = _load("faculty_login.html")
FACULTY_PASSWORD_PAGE = _load("faculty_password.html")
FACULTY_ADMIN_PAGE = _load("faculty_admin.html")
FACULTY_PAGE = _load("faculty.html")
PRIVACY_POLICY_PAGE = _load("privacy.html")
TERMS_OF_SERVICE_PAGE = _load("terms.html")


def render_admin_login_page(query: str) -> str:
    return _load("admin_login.html").replace("__AUTH_QUERY__", query)


def render_error_page(status_code: int, detail: Any) -> str:
    titles = {
        400: "That request didn't work",
        401: "You need to sign in",
        403: "You don't have permission",
        404: "This page is missing",
        405: "That action isn't available",
        409: "This request conflicts with the current state",
        422: "Check the information and try again",
        429: "Too many requests",
        502: "Discord couldn't complete the request",
        503: "This service is temporarily unavailable",
    }
    values = {
        "__STATUS_CODE__": str(status_code),
        "__TITLE__": html.escape(titles.get(status_code, "Something went wrong"), quote=True),
        "__MESSAGE__": html.escape(str(detail), quote=True),
        "__DESTINATION__": "/login" if status_code == 401 else "/",
        "__LINK_LABEL__": "Sign in with Discord" if status_code == 401 else "Back to dashboard",
    }
    page = _load("error.html")
    for marker, value in values.items():
        page = page.replace(marker, value)
    return page
