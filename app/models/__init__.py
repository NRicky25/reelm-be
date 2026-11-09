"""Database tables. Import everything here so Alembic sees all models."""

from app.models.catalog import Episode, Genre, Season, Title, TitleGenre, TitleType
from app.models.video import Video, VideoProvider, VideoStatus

__all__ = [
    "Episode",
    "Genre",
    "Season",
    "Title",
    "TitleGenre",
    "TitleType",
    "Video",
    "VideoProvider",
    "VideoStatus",
]
