"""Read-only Google tools, a local briefing, and durable reminder delivery."""

import asyncio
import json
import os
import stat
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Protocol
from urllib.parse import quote

from stage_01_model_tools_py.transport_py import Http, ProviderFormatError

from .storage_py import Inbox, Store, utc_text

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"
GOOGLE = "https://www.googleapis.com"


class OAuthError(RuntimeError):
    """Errors safe to show without printing token files or provider responses."""


def save_token(path: Path, serialized: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # mkstemp creates a private file. Write completely, then atomically replace
    # the token so a crash cannot leave a half-written credentials file.
    fd, temporary = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OAuth:
    def __init__(self, token_path: Path, gmail: bool = False):
        self.token_path = token_path
        self.scopes = [CALENDAR_SCOPE, GMAIL_SCOPE] if gmail else [CALENDAR_SCOPE]
        self.lock = Lock()

    def authorize(self, credentials_path: Path) -> None:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from oauthlib.oauth2 import OAuth2Error

        if credentials_path.resolve() == self.token_path.resolve():
            raise OAuthError("client credentials and token files must be different")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), self.scopes)
            credentials = flow.run_local_server(
                host="127.0.0.1", port=0, timeout_seconds=180, prompt="consent"
            )
            if not credentials.refresh_token:
                raise OAuthError("Google did not return a refresh token; authorize again")
            save_token(self.token_path, credentials.to_json())
        except (OSError, ValueError, OAuth2Error, Warning):
            raise OAuthError(
                "Google authorization failed; check the Desktop client and consent setup"
            ) from None

    async def access_token(self) -> str:
        return await asyncio.to_thread(self._load_or_refresh)

    def _load_or_refresh(self) -> str:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        with self.lock:
            try:
                mode = self.token_path.stat().st_mode
                if not stat.S_ISREG(mode) or stat.S_IMODE(mode) & 0o077:
                    raise OAuthError("OAuth token must be a private file; use chmod 600 on it")
                data = json.loads(self.token_path.read_text())
                if not set(self.scopes).issubset(data.get("scopes") or []):
                    raise OAuthError(
                        "required scopes are missing; run authorize with the same flags"
                    )
                credentials = Credentials.from_authorized_user_info(data, self.scopes)
                if not credentials.valid:
                    # Refresh is explicit and happens before a Google resource call.
                    credentials.refresh(partial(Request(), timeout=10))
                    save_token(self.token_path, credentials.to_json())
                if not credentials.token:
                    raise OAuthError("token is missing; run authorize")
                return credentials.token
            except (OSError, ValueError, TypeError, AttributeError, GoogleAuthError):
                raise OAuthError(
                    "Google token is missing, invalid, or expired; run authorize"
                ) from None


class Calendar(Protocol):
    async def events(self, now: datetime) -> list[dict]: ...


class GoogleWorkspace:
    def __init__(self, http: Http, oauth: OAuth):
        self.http = http
        self.oauth = oauth

    async def get(self, path: str, **params) -> dict:
        token = await self.oauth.access_token()
        return await self.http.json(
            "Google Workspace",
            "GET",
            f"{GOOGLE}/{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            retry=True,
        )

    async def events(self, now: datetime) -> list[dict]:
        data = await self.get(
            "calendar/v3/calendars/primary/events",
            timeMin=utc_text(now),
            maxResults=8,
            singleEvents="true",
            orderBy="startTime",
        )
        try:
            return [
                {
                    "title": item.get("summary", "(untitled)"),
                    "start": item["start"].get("dateTime") or item["start"]["date"],
                }
                for item in data.get("items", [])[:8]
            ]
        except (KeyError, AttributeError, TypeError):
            raise ProviderFormatError("invalid calendar events") from None

    async def inbox_headers(self) -> list[dict]:
        data = await self.get("gmail/v1/users/me/messages", labelIds="INBOX", maxResults=5)

        async def metadata(item):
            identifier = quote(item["id"], safe="")
            return await self.get(
                f"gmail/v1/users/me/messages/{identifier}",
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )

        try:
            responses = await asyncio.gather(
                *(metadata(item) for item in data.get("messages", [])[:5])
            )
            return [
                {
                    header["name"].casefold(): header["value"]
                    for header in response["payload"].get("headers", [])
                    if header["name"].casefold() in {"from", "subject"}
                }
                for response in responses
            ]
        except (KeyError, AttributeError, TypeError):
            raise ProviderFormatError("invalid Gmail metadata") from None


class DemoCalendar:
    async def events(self, now: datetime) -> list[dict]:
        return [{"title": "Agent design review (fixture)", "start": utc_text(now)}]


def outfit(temperature_c: float) -> str:
    if temperature_c < 10:
        return "Wear a warm coat and layers."
    if temperature_c < 19:
        return "Bring a light jacket."
    return "A light layer should be enough."


async def briefing(
    calendar: Calendar,
    weather: Callable[[str], Awaitable[dict]],
    city: str,
    now: datetime,
) -> dict:
    async with asyncio.TaskGroup() as group:
        events = group.create_task(calendar.events(now))
        current = group.create_task(weather(city))
    reading = current.result()
    # Calendar and weather stay in this process. The outfit decision needs only temperature.
    return {
        "events": events.result(),
        "weather": reading,
        "outfit": outfit(reading["temperature_c"]),
    }


def send_due(store: Store, inbox: Inbox, now: datetime) -> int:
    inserted = 0
    for reminder in store.due(now):
        payload = json.dumps({"type": "reminder", "text": reminder.text, "due_at": reminder.due_at})
        # Use the receiver's same replay contract as the report outbox.
        inserted += inbox.deliver(f"reminder:{reminder.key}", payload)
        store.acknowledge_reminder(reminder.key)
    return inserted
