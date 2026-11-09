"""The ONE place that decides whether a stored Video is playable and builds its URL.

Switching video hosts later (e.g. Mux -> Bunny) only changes this file.
"""

from sqlalchemy import ColumnElement, and_, or_

from app.models import Video, VideoProvider, VideoStatus


def playback_url(video: Video | None) -> str | None:
    if video is None:
        return None
    if video.provider == VideoProvider.external:
        # A plain URL is playable as soon as it's set; there's no processing step.
        return video.url or None
    if (
        video.provider == VideoProvider.mux
        and video.status == VideoStatus.ready
        and video.playback_id
    ):
        return f"https://stream.mux.com/{video.playback_id}.m3u8"
    return None


def playable_clause() -> ColumnElement[bool]:
    """SQL version of `playback_url(v) is not None`, for filtering in queries."""
    return or_(
        and_(
            Video.provider == VideoProvider.external,
            Video.url.is_not(None),
            Video.url != "",
        ),
        and_(
            Video.provider == VideoProvider.mux,
            Video.status == VideoStatus.ready,
            Video.playback_id.is_not(None),
        ),
    )
