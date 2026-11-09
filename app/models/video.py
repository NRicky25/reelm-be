"""A playable video, stored provider-neutrally.

We never store a full stream URL for hosted providers: we store the provider
and its IDs, and build the URL in app/services/video_url.py. Switching from
Mux to another host later only changes that one function.

Each episode / movie gets its own Video row (never shared), so replacing or
deleting one video can't affect other episodes.
"""

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


class VideoProvider(str, enum.Enum):
    mux = "mux"
    external = "external"  # a plain URL (e.g. the public sample streams)


class VideoStatus(str, enum.Enum):
    waiting = "waiting"  # upload created, file not processed yet
    ready = "ready"
    errored = "errored"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    provider: Mapped[VideoProvider] = mapped_column(
        Enum(VideoProvider, native_enum=False, length=20)
    )
    status: Mapped[VideoStatus] = mapped_column(
        Enum(VideoStatus, native_enum=False, length=20),
        default=VideoStatus.waiting,
        server_default=VideoStatus.waiting.value,
    )

    # Mux (unique, so repeated webhooks always update the same row)
    upload_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    asset_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    playback_id: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)

    # external
    url: Mapped[str | None] = mapped_column(String(500))

    # Where a new upload goes once Mux says it's ready. Until then the episode /
    # movie keeps playing its current video; the swap happens in one step.
    pending_episode_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "episodes.id",
            ondelete="SET NULL",
            name="fk_videos_pending_episode",
            use_alter=True,
        ),
        index=True,
    )
    pending_title_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "titles.id",
            ondelete="SET NULL",
            name="fk_videos_pending_title",
            use_alter=True,
        ),
        index=True,
    )

    duration_sec: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )

    def __str__(self) -> str:
        ref = self.playback_id or self.url or self.upload_id or self.id
        return f"{self.provider.value}: {ref[:60]} ({self.status.value})"

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )
