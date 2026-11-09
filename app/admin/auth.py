"""Password login for the admin panel (single admin account from settings)."""

import asyncio
import secrets

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.config import Settings

SESSION_KEY = "reelm_admin"


class AdminAuth(AuthenticationBackend):
    def __init__(self, settings: Settings) -> None:
        if settings.admin_password is None:
            raise ValueError("ADMIN_PASSWORD must be set to enable the admin panel")
        secret = (
            settings.admin_secret_key.get_secret_value()
            if settings.admin_secret_key
            else secrets.token_urlsafe(32)
        )
        super().__init__(
            secret_key=secret,
            session_cookie="reelm_admin_session",
            same_site="strict",  # the cookie is never sent from other sites (CSRF protection)
            https_only=settings.admin_cookie_secure,
            max_age=8 * 60 * 60,  # log in again after 8 hours
        )
        self._username = settings.admin_username.encode()
        self._password = settings.admin_password.get_secret_value().encode()

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username", "")).encode()
        password = str(form.get("password", "")).encode()
        # constant-time comparisons (both always run) so timing reveals nothing
        ok_user = secrets.compare_digest(username, self._username)
        ok_pass = secrets.compare_digest(password, self._password)
        if ok_user and ok_pass:
            request.session.clear()
            request.session[SESSION_KEY] = True
            return True
        await asyncio.sleep(1)  # slow down password guessing
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return bool(request.session.get(SESSION_KEY))
