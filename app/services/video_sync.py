"""Keep our `videos` rows in step with Mux.

Updates arrive two ways, and both end up here:
- Mux webhooks (production): /webhooks/mux
- polling from the admin upload page (works locally without a public URL)
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Episode, Title, Video, VideoProvider, VideoStatus
from app.services.mux import MuxClient

log = logging.getLogger(__name__)


def _public_playback_id(asset: dict[str, Any]) -> str | None:
    for pb in asset.get("playback_ids") or []:
        if pb.get("policy") == "public":
            return pb.get("id")
    return None


def _copy_duration_to_owner(session: Session, video: Video) -> None:
    """Fill in the episode / movie length if it hasn't been entered."""
    if not video.duration_sec:
        return
    seconds = round(video.duration_sec)
    for owner in (
        session.scalar(select(Episode).where(Episode.video_id == video.id)),
        session.scalar(select(Title).where(Title.video_id == video.id)),
    ):
        if owner is not None and not owner.duration_sec:
            owner.duration_sec = seconds


def apply_asset(session: Session, video: Video, asset: dict[str, Any]) -> None:
    video.asset_id = asset.get("id") or video.asset_id
    status = asset.get("status")
    if status == "ready":
        video.playback_id = _public_playback_id(asset)
        video.duration_sec = asset.get("duration")
        video.status = VideoStatus.ready if video.playback_id else VideoStatus.errored
        video.error_message = (
            None if video.playback_id else "Mux asset has no public playback ID"
        )
        _copy_duration_to_owner(session, video)
    elif status == "errored":
        video.status = VideoStatus.errored
        messages = (asset.get("errors") or {}).get("messages") or [
            "Mux couldn't process this file"
        ]
        video.error_message = "; ".join(messages)


def apply_upload(video: Video, upload: dict[str, Any]) -> None:
    status = upload.get("status")
    if upload.get("asset_id"):
        video.asset_id = upload["asset_id"]
    if status in ("errored", "cancelled", "timed_out"):
        video.status = VideoStatus.errored
        error = (upload.get("error") or {}).get("message")
        video.error_message = error or f"Upload {status.replace('_', ' ')}"


def sync_from_mux(session: Session, mux: MuxClient, video: Video) -> Video:
    """Ask Mux for the latest state of this video (used by the upload page)."""
    if video.provider != VideoProvider.mux or video.status == VideoStatus.ready:
        return video
    if not video.asset_id and video.upload_id:
        apply_upload(video, mux.get_upload(video.upload_id))
    if video.asset_id and video.status != VideoStatus.errored:
        apply_asset(session, video, mux.get_asset(video.asset_id))
    session.commit()
    return video


def handle_webhook_event(session: Session, event: dict[str, Any]) -> str:
    """Apply a Mux webhook event. Returns a short note for logs/tests."""
    kind = event.get("type", "")
    data = event.get("data") or {}

    if kind.startswith("video.upload."):
        video = session.scalar(select(Video).where(Video.upload_id == data.get("id")))
        if video is None:
            return "ignored: unknown upload"
        apply_upload(video, data)
    elif kind in ("video.asset.ready", "video.asset.errored", "video.asset.created"):
        video = session.scalar(select(Video).where(Video.asset_id == data.get("id")))
        if video is None and data.get("upload_id"):
            video = session.scalar(
                select(Video).where(Video.upload_id == data["upload_id"])
            )
        if video is None:
            return "ignored: unknown asset"
        apply_asset(session, video, data)
    else:
        return f"ignored: {kind or 'no type'}"

    session.commit()
    return f"applied: {kind}"
