"""Admin panel (SQLAdmin) mounted at /admin, behind a password."""

import logging

from fastapi import FastAPI
from sqladmin import Admin
from sqlalchemy.engine import Engine

from app.admin.auth import AdminAuth
from app.admin.views import ALL_VIEWS
from app.config import Settings

log = logging.getLogger(__name__)


def setup_admin(app: FastAPI, engine: Engine, settings: Settings) -> Admin | None:
    """Mount the admin panel, or skip it (with a warning) if no password is configured."""
    if settings.admin_password is None:
        log.warning("Admin panel disabled: set ADMIN_PASSWORD in .env to enable /admin")
        return None
    admin = Admin(
        app,
        engine,
        title="Reelm Admin",
        authentication_backend=AdminAuth(settings),
    )
    for view in ALL_VIEWS:
        admin.add_view(view)
    return admin
