"""Password login for the admin panel (single admin account from settings)."""

import asyncio
import hashlib
import secrets
import time
from collections import defaultdict, deque

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.config import Settings

SESSION_KEY = "reelm_admin"
MIN_PASSWORD_LENGTH = 12

# Brute-force protection: after MAX_FAILURES wrong passwords from one address
# within WINDOW_SEC, refuse all logins from it until the window has passed.
MAX_FAILURES = 10
WINDOW_SEC = 15 * 60


def admin_password(settings: Settings) -> str | None:
    """The configured password, or None if unset/blank (admin stays off)."""
    pw = settings.admin_password.get_secret_value() if settings.admin_password else ""
    return pw if pw.strip() else None


def _digest(value: str) -> bytes:
    # Hash first so the comparison doesn't depend on the input length.
    return hashlib.sha256(value.encode()).digest()


class AdminAuth(AuthenticationBackend):
    def __init__(self, settings: Settings) -> None:
        password = admin_password(settings)
        if password is None:
            raise ValueError("ADMIN_PASSWORD must be set to enable the admin panel")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"ADMIN_PASSWORD must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if settings.admin_cookie_secure and settings.admin_secret_key is None:
            raise ValueError(
                "Set ADMIN_SECRET_KEY when ADMIN_COOKIE_SECURE is on (production)"
            )

        secret = (
            settings.admin_secret_key.get_secret_value()
            if settings.admin_secret_key
            else secrets.token_urlsafe(32)
        )
        super().__init__(
            secret_key=secret,
            session_cookie="reelm_admin",  # renamed from reelm_admin_session (old cookies had path=/)
            path="/admin",  # only sent to the admin, not to every API request
            same_site="strict",  # never sent from other sites
            https_only=settings.admin_cookie_secure,
            max_age=8 * 60 * 60,  # log in again after 8 hours
        )
        self._username = _digest(settings.admin_username)
        self._password = _digest(password)
        # Stored in the session: changing the password logs out existing sessions.
        self._session_token = hashlib.sha256(
            b"reelm-admin:" + self._password
        ).hexdigest()
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)

    def _locked_out(self, client: str) -> bool:
        attempts = self._failures[client]
        cutoff = time.monotonic() - WINDOW_SEC
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return len(attempts) >= MAX_FAILURES

    async def login(self, request: Request) -> bool:
        client = request.client.host if request.client else "unknown"
        if self._locked_out(client):
            await asyncio.sleep(1)
            return False

        form = await request.form()
        ok_user = secrets.compare_digest(
            _digest(str(form.get("username", ""))), self._username
        )
        ok_pass = secrets.compare_digest(
            _digest(str(form.get("password", ""))), self._password
        )
        if ok_user and ok_pass:
            self._failures.pop(client, None)
            request.session.clear()
            request.session[SESSION_KEY] = self._session_token
            return True

        self._failures[client].append(time.monotonic())
        await asyncio.sleep(1)  # slow down password guessing
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        token = request.session.get(SESSION_KEY)
        return isinstance(token, str) and secrets.compare_digest(
            token, self._session_token
        )
