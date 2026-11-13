"""Admin panel (SQLAdmin) mounted at /admin, behind a password."""

import logging
from urllib.parse import urlsplit

from fastapi import FastAPI
from sqladmin import Admin
from sqlalchemy.engine import Engine
from starlette.datastructures import FormData
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.admin.auth import AdminAuth, admin_password
from app.admin.views import ALL_VIEWS, configure_views
from app.config import Settings

log = logging.getLogger(__name__)
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class SameOriginMiddleware:
    """Reject admin form posts / deletes that come from another origin.

    SQLAdmin forms have no CSRF tokens. The cookie is SameSite=strict, but other
    ports/subdomains of the same site could still post; browsers always send
    Origin (or Referer) on these requests, so we require it to match.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in SAFE_METHODS:
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            source = headers.get("origin") or headers.get("referer")
            host = headers.get("host", "")
            if source and urlsplit(source).netloc != host:
                await PlainTextResponse("Cross-origin request blocked", 403)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)


class ReelmAdmin(Admin):
    """SQLAdmin with plain form handling for our "Upload …" file fields.

    SQLAdmin assumes every file input is a file column on the model and, when
    editing with no file chosen, calls getattr(model, "poster_file") - which
    doesn't exist (our files go to the media folder, see ImageUploadMixin) -> 500.
    We pass the form through as-is; the mixin ignores empty file inputs.
    """

    @staticmethod
    async def _handle_form_data(request: Request, obj: object = None) -> FormData:
        return FormData((await request.form()).multi_items())


def setup_admin(app: FastAPI, engine: Engine, settings: Settings) -> ReelmAdmin | None:
    """Mount the admin panel, or skip it (with a warning) if no password is configured."""
    if admin_password(settings) is None:
        log.warning("Admin panel disabled: set ADMIN_PASSWORD in .env to enable /admin")
        return None
    admin = ReelmAdmin(
        app,
        engine,
        title="Reelm Admin",
        authentication_backend=AdminAuth(settings),
        middlewares=[Middleware(SameOriginMiddleware)],
    )
    configure_views(settings)
    for view in ALL_VIEWS:
        admin.add_view(view)
    return admin
