"""Reelm API."""

from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.admin import setup_admin
from app.config import get_settings
from app.db import engine, get_db
from app.routers import catalog

app = FastAPI(title="Reelm API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET"],
    allow_headers=["*"],
)


app.include_router(catalog.router)
setup_admin(app, engine, get_settings())


@app.get("/health", tags=["system"])
def health(db: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
    """Liveness + database check."""
    db.execute(text("select 1"))
    return {"status": "ok"}
