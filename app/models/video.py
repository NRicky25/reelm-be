"""A playable video: a link to an HLS (.m3u8) or MP4 file, plus its length.

Videos are encoded with reelm-encoder and hosted on Cloudflare R2 (or any
static host); the row just stores the public link. The player URL is built in
app/services/video_url.py, so a different host later only changes that file.

Each episode / movie gets its own Video row (never shared), so replacing or
deleting one video can't affect other episodes.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, JSONList


def _now() -> datetime:
    return datetime.now(UTC)


class VideoProvider(str, enum.Enum):
    # a public link, e.g. R2: <bucket URL>/videos/<folder>/master.m3u8
    external = "external"
    # Left over from before the switch to R2. Such a row still loads (so pages
    # don't crash) but never plays; migration c3f8a1d2e5b7 won't run until none
    # remain. Replace it with an R2 video in the admin, then delete it.
    mux = "mux"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[VideoProvider] = mapped_column(
        Enum(VideoProvider, native_enum=False, length=20),
        default=VideoProvider.external,
    )
    url: Mapped[str | None] = mapped_column(String(500))
    duration_sec: Mapped[float | None] = mapped_column(Float)
    # Language codes of the subtitle files next to the playlist, e.g. ["en"] for
    # .../<folder>/en.vtt (R2 Uploader puts them there).
    subtitles: Mapped[list[str]] = mapped_column(
        JSONList, default=list, server_default=text("'[]'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    def __str__(self) -> str:
        if self.provider == VideoProvider.mux:
            return f"Old Mux video {self.id[:8]} (not playable: replace it)"
        return self.url or f"(no link) {self.id}"
