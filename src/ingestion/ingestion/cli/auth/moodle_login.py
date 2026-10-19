"""Moodle login — username/password → wstoken + user_id."""

from __future__ import annotations

import httpx
import questionary
from rich.console import Console

from ingestion.cli.display import SYM_FAIL, SYM_OK, SYM_PENDING, print_status
from ingestion.cli.state import save_credentials
from ingestion.config import MoodleConfig

console = Console(stderr=True)

MAX_ATTEMPTS = 3


def run_moodle_login() -> None:
    """Prompt for username/password and acquire wstoken via /login/token.php."""
    config = MoodleConfig()
    base_url = config.base_url.rstrip("/")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        username = questionary.text("Moodle username:").ask()
        if username is None:
            return
        password = questionary.password("Moodle password:").ask()
        if password is None:
            return

        print_status(SYM_PENDING, f"Authenticating with {base_url}...")

        try:
            token = _acquire_token(base_url, username, password)
        except AuthError as exc:
            print_status(SYM_FAIL, f"Invalid login: {exc} (attempt {attempt}/{MAX_ATTEMPTS})")
            if attempt == MAX_ATTEMPTS:
                console.print("  [red]Max attempts reached.[/red]")
                return
            console.print()
            continue

        print_status(SYM_OK, f"Token acquired (wstoken: {token[:8]}...)")

        user_id, _site_name, courses = _fetch_user_info(base_url, token)
        print_status(SYM_OK, f"User ID: {user_id}")

        if courses:
            print_status(SYM_OK, f"Found {len(courses)} enrolled courses")
            for c in courses[:8]:
                console.print(f"    [dim]•[/dim] {c}")
            if len(courses) > 8:
                console.print(f"    [dim]... and {len(courses) - 8} more[/dim]")

        save_credentials("moodle", {"token": token, "user_id": user_id})
        console.print("  Saved to ~/.config/ingest/credentials.json")
        return


class AuthError(Exception):
    pass


def _acquire_token(base_url: str, username: str, password: str) -> str:
    """POST to /login/token.php to get wstoken."""
    url = f"{base_url}/login/token.php"
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            url,
            data={
                "username": username,
                "password": password,
                "service": "moodle_mobile_app",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if "token" in data:
        return data["token"]

    error = data.get("error", "Unknown error")
    raise AuthError(error)


def _fetch_user_info(base_url: str, token: str) -> tuple[int, str, list[str]]:
    """Fetch user_id, site name, and enrolled courses using wstoken."""
    endpoint = f"{base_url}/webservice/rest/server.php"

    with httpx.Client(timeout=30) as client:
        # Get site info (user_id, sitename)
        resp = client.post(
            endpoint,
            data={
                "wstoken": token,
                "wsfunction": "core_webservice_get_site_info",
                "moodlewsrestformat": "json",
            },
        )
        site_info = resp.json()
        user_id = site_info.get("userid", 0)
        site_name = site_info.get("sitename", "")

        # Get enrolled courses
        resp = client.post(
            endpoint,
            data={
                "wstoken": token,
                "wsfunction": "core_enrol_get_users_courses",
                "moodlewsrestformat": "json",
                "userid": user_id,
            },
        )
        courses_data = resp.json()

    course_names = []
    if isinstance(courses_data, list):
        course_names = [c.get("fullname", "") for c in courses_data if c.get("fullname")]

    return user_id, site_name, course_names
