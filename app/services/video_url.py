"""The ONE place that turns a stored Video into a playable URL.

Switching video hosts later (e.g. Mux -> Bunny) only changes this function.
"""

from app.models import Video, VideoProvider, VideoStatus


def playback_url(video: Video | None) -> str | None:
    if video is None or video.status != VideoStatus.ready:
        return None
    if video.provider == VideoProvider.mux and video.playback_id:
        return f"https://stream.mux.com/{video.playback_id}.m3u8"
    if video.provider == VideoProvider.external and video.url:
        return video.url
    return None
