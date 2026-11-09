"""Keep our `videos` rows in step with Mux, and swap in new uploads safely.

Updates arrive two ways, and both end up here:
- Mux webhooks (production): /webhooks/mux
- polling from the admin upload page (works locally without a public URL)

A new upload is stored with a *pending* target (episode or movie). The target
keeps playing its current video until Mux reports the new one `ready`; then the
target is pointed at the new video and the old one is deleted, in that order.

Mux HTTP calls never happen while a database transaction is open.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.models import Episode, Title, Video, VideoProvider, VideoStatus
from app.services.mux import MuxClient, MuxError

log = logging.getLogger(__name__)


# ───────── applying Mux data to a row ─────────
def _public_playback_id(asset: dict[str, Any]) -> str | None:
    for pb in asset.get("playback_ids") or []:
        if pb.get("policy") == "public":
            return pb.get("id")
    return None


def _activate(session: Session, video: Video) -> list[str]:
    """Point the pending episode/movie at this (ready) video. Returns replaced video ids."""
    owner: Episode | Title | None = None
    if video.pending_episode_id:
        owner = session.get(Episode, video.pending_episode_id)
    elif video.pending_title_id:
        owner = session.get(Title, video.pending_title_id)
    video.pending_episode_id = video.pending_title_id = None
    if owner is None:
        return []
    replaced = [owner.video_id] if owner.video_id and owner.video_id != video.id else []
    owner.video_id = video.id
    if video.duration_sec:  # a new video may have a different length
        owner.duration_sec = round(video.duration_sec)
    return replaced


def apply_upload(video: Video, upload: dict[str, Any]) -> None:
    status = upload.get("status")
    if upload.get("asset_id"):
        video.asset_id = upload["asset_id"]
    if (
        status in ("errored", "cancelled", "timed_out")
        and video.status == VideoStatus.waiting
    ):
        video.status = VideoStatus.errored
        error = (upload.get("error") or {}).get("message")
        video.error_message = error or f"Upload {status.replace('_', ' ')}"
        video.pending_episode_id = video.pending_title_id = None  # nothing to swap in


def apply_asset(session: Session, video: Video, asset: dict[str, Any]) -> list[str]:
    """Apply an asset's state. Returns ids of videos replaced by this one (to delete)."""
    video.asset_id = asset.get("id") or video.asset_id
    status = asset.get("status")
    if status == "ready" and video.status != VideoStatus.ready:
        video.playback_id = _public_playback_id(asset)
        video.duration_sec = asset.get("duration")
        if video.playback_id:
            video.status = VideoStatus.ready
            video.error_message = None
            return _activate(session, video)
        video.status = VideoStatus.errored
        video.error_message = "Mux asset has no public playback ID"
    elif status == "errored":
        video.status = VideoStatus.errored
        messages = (asset.get("errors") or {}).get("messages") or [
            "Mux couldn't process this file"
        ]
        video.error_message = "; ".join(messages)
        video.pending_episode_id = video.pending_title_id = None
    # "preparing" (or a late "created" event after ready) changes nothing
    return []


# ───────── deleting ─────────
@dataclass
class _MuxRef:
    asset_id: str | None
    upload_id: str | None


def delete_videos_everywhere(
    session: Session, video_ids: list[str], mux: MuxClient | None
) -> None:
    """Delete video rows, then (after the commit) their Mux assets / pending uploads.

    Database first: if the commit fails, nothing on Mux has been touched. A Mux
    failure afterwards leaves an orphan asset, which is logged for cleanup.
    """
    if not video_ids:
        return
    videos = list(session.scalars(select(Video).where(Video.id.in_(video_ids))))
    refs = [
        _MuxRef(v.asset_id, v.upload_id)
        for v in videos
        if v.provider == VideoProvider.mux and (v.asset_id or v.upload_id)
    ]
    for v in videos:
        session.delete(v)
    session.commit()
    if mux:
        for ref in refs:
            remove_from_mux(mux, ref.asset_id, ref.upload_id)


def remove_from_mux(
    mux: MuxClient, asset_id: str | None, upload_id: str | None
) -> None:
    """Delete an asset, or cancel an upload (deleting the asset it already made)."""
    try:
        if not asset_id and upload_id:
            try:
                mux.cancel_upload(upload_id)
            except MuxError as e:
                if e.status_code not in (400, 404, 409):  # e.g. already finished
                    raise
            asset_id = mux.get_upload(upload_id).get("asset_id")
        if asset_id:
            mux.delete_asset(asset_id)
    except MuxError as e:
        log.warning(
            "Couldn't remove Mux asset=%s upload=%s: %s (delete it in the Mux dashboard)",
            asset_id,
            upload_id,
            e,
        )


# ───────── polling ─────────
def sync_from_mux(
    sessions: sessionmaker, mux: MuxClient, video_id: str
) -> dict[str, Any] | None:
    """Ask Mux for the latest state of one video and apply it (admin upload page)."""
    with sessions() as s:
        video = s.get(Video, video_id)
        if video is None:
            return None
        provider, status = video.provider, video.status
        upload_id, asset_id = video.upload_id, video.asset_id

    upload = asset = None
    if provider == VideoProvider.mux and status == VideoStatus.waiting:
        if not asset_id and upload_id:
            upload = mux.get_upload(upload_id)
            asset_id = upload.get("asset_id")
        if asset_id:
            asset = mux.get_asset(asset_id)

    replaced: list[str] = []
    with sessions() as s:
        video = s.get(Video, video_id)
        if video is None:
            return None
        if upload:
            apply_upload(video, upload)
        if asset and video.status != VideoStatus.errored:
            replaced = apply_asset(s, video, asset)
        s.commit()
        result = {
            "status": video.status.value,
            "error": video.error_message,
            "playback_id": video.playback_id,
            "duration_sec": video.duration_sec,
        }
        delete_videos_everywhere(s, replaced, mux)
    return result


def sync_stale_uploads(
    sessions: sessionmaker, mux: MuxClient, *, older_than: timedelta, limit: int = 10
) -> int:
    """Catch up on uploads left 'waiting' (closed tab, no webhooks). Returns how many were checked."""
    cutoff = datetime.now(UTC) - older_than
    with sessions() as s:
        ids = list(
            s.scalars(
                select(Video.id)
                .where(
                    Video.provider == VideoProvider.mux,
                    Video.status == VideoStatus.waiting,
                )
                .where(Video.created_at < cutoff)
                .order_by(Video.created_at)
                .limit(limit)
            )
        )
    for vid in ids:
        try:
            sync_from_mux(sessions, mux, vid)
        except MuxError as e:
            log.warning("Couldn't sync video %s with Mux: %s", vid, e)
    return len(ids)


# ───────── webhooks ─────────
def handle_webhook_event(
    session: Session, event: dict[str, Any], mux: MuxClient | None = None
) -> str:
    """Apply a Mux webhook event. Returns a short note for logs/tests."""
    kind = event.get("type", "")
    data = event.get("data") or {}
    replaced: list[str] = []

    if kind.startswith("video.upload."):
        video = session.scalar(select(Video).where(Video.upload_id == data.get("id")))
        if video is None:
            return "ignored: unknown upload"
        apply_upload(video, data)
    elif kind.startswith("video.asset."):
        video = session.scalar(select(Video).where(Video.asset_id == data.get("id")))
        if video is None and data.get("upload_id"):
            video = session.scalar(
                select(Video).where(Video.upload_id == data["upload_id"])
            )
        if video is None:
            return "ignored: unknown asset"
        if kind == "video.asset.deleted":
            video.status = VideoStatus.errored
            video.error_message = "Asset deleted in Mux"
            video.playback_id = None
        elif kind in (
            "video.asset.created",
            "video.asset.ready",
            "video.asset.errored",
        ):
            replaced = apply_asset(session, video, data)
        else:
            return f"ignored: {kind}"
    else:
        return f"ignored: {kind or 'no type'}"

    session.commit()
    delete_videos_everywhere(session, replaced, mux)
    return f"applied: {kind}"
