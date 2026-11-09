"""Incoming webhooks from Mux (signature-verified)."""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.services.mux import verify_webhook
from app.services.video_sync import handle_webhook_event

router = APIRouter(tags=["webhooks"])
log = logging.getLogger(__name__)


@router.post("/webhooks/mux", status_code=204)
async def mux_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    mux_signature: Annotated[str | None, Header()] = None,
) -> None:
    secret = get_settings().mux_webhook_secret
    if secret is None:
        raise HTTPException(
            503, "Mux webhooks aren't configured (set MUX_WEBHOOK_SECRET)"
        )
    body = await request.body()
    if not verify_webhook(body, mux_signature, secret.get_secret_value()):
        raise HTTPException(401, "Invalid signature")
    try:
        event = json.loads(body)
    except ValueError as e:
        raise HTTPException(400, "Body isn't JSON") from e
    note = handle_webhook_event(db, event)
    log.info("Mux webhook %s: %s", event.get("type"), note)
