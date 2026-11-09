"""The ONE place that turns a stored Video into a playable URL.

Switching video hosts later (e.g. Mux -> Bunny) only changes this function.
"""

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
