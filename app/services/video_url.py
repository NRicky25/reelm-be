"""The ONE place that decides whether a stored Video is playable and builds its URL.

Switching video hosts later only changes this file.
"""

from sqlalchemy import ColumnElement, and_

from app.models import Video, VideoProvider


def playback_url(video: Video | None) -> str | None:
    if video is None:
        return None
    if video.provider in (
        VideoProvider.external,
        None,
    ):  # None: new row, default not applied yet
        # A plain link is playable as soon as it's set; there's no processing step.
        return video.url or None
    return None


def subtitle_tracks(video: Video | None) -> list[dict[str, str]]:
    """The video's subtitle files: <lang>.vtt in the same folder as its playlist."""
    url = playback_url(video)
    if not url or not video or not video.subtitles:
        return []
    folder = url.split("?", 1)[0].rsplit("/", 1)[0]
    return [{"lang": lang, "url": f"{folder}/{lang}.vtt"} for lang in video.subtitles]


def playable_clause() -> ColumnElement[bool]:
    """SQL version of `playback_url(v) is not None`, for filtering in queries."""
    return and_(
        Video.provider == VideoProvider.external,
        Video.url.is_not(None),
        Video.url != "",
    )
