"""Catalogue tables: titles, genres, seasons, episodes.

Kept portable between SQLite (local) and Postgres (Supabase): lists are
stored as JSON rather than Postgres-only ARRAY columns.
"""

import enum
from datetime import date

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.video import Video


class TitleType(str, enum.Enum):
    anime = "anime"
    movie = "movie"
    series = "series"


class Genre(Base):
    __tablename__ = "genres"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(60), unique=True)


class TitleGenre(Base):
    """Link table that also keeps genre order (the first genre is the 'main' one)."""

    __tablename__ = "title_genres"

    title_id: Mapped[str] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"), primary_key=True
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genres.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)

    genre: Mapped[Genre] = relationship(lazy="joined")


class Title(Base):
    __tablename__ = "titles"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    type: Mapped[TitleType] = mapped_column(
        Enum(TitleType, native_enum=False, length=10), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    year: Mapped[int] = mapped_column(Integer)
    age_rating: Mapped[str] = mapped_column(String(10), default="All")
    sub: Mapped[bool | None] = mapped_column(Boolean)
    dub: Mapped[bool | None] = mapped_column(Boolean)
    audio_languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    subtitle_languages: Mapped[list[str]] = mapped_column(JSON, default=list)
    colors: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["#3a3a4a", "#22222b"]
    )
    poster_url: Mapped[str | None] = mapped_column(String(500))
    backdrop_url: Mapped[str | None] = mapped_column(String(500))
    poster_focus_x: Mapped[int | None] = mapped_column(Integer)
    featured: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    staff_pick: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    added_at: Mapped[date] = mapped_column(Date, index=True)
    studio: Mapped[str] = mapped_column(String(120), default="")
    cast: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Movies only
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    intro_start_sec: Mapped[int | None] = mapped_column(Integer)
    intro_end_sec: Mapped[int | None] = mapped_column(Integer)
    video_id: Mapped[str | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL")
    )
    video: Mapped[Video | None] = relationship(lazy="joined")

    genre_links: Mapped[list[TitleGenre]] = relationship(
        order_by=TitleGenre.position, cascade="all, delete-orphan", lazy="selectin"
    )
    seasons: Mapped[list["Season"]] = relationship(
        back_populates="title",
        order_by="Season.number",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    @property
    def genres(self) -> list[str]:
        return [link.genre.name for link in self.genre_links]

    def __repr__(self) -> str:
        return f"<Title {self.slug}>"


class Season(Base):
    __tablename__ = "seasons"
    __table_args__ = (UniqueConstraint("title_id", "number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_id: Mapped[str] = mapped_column(
        ForeignKey("titles.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)

    title: Mapped[Title] = relationship(back_populates="seasons")
    episodes: Mapped[list["Episode"]] = relationship(
        back_populates="season",
        order_by="Episode.number",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class Episode(Base):
    __tablename__ = "episodes"
    __table_args__ = (UniqueConstraint("season_id", "number"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    season_id: Mapped[int] = mapped_column(
        ForeignKey("seasons.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    release_date: Mapped[date] = mapped_column(Date, index=True)
    thumb_url: Mapped[str | None] = mapped_column(String(500))
    intro_start_sec: Mapped[int | None] = mapped_column(Integer)
    intro_end_sec: Mapped[int | None] = mapped_column(Integer)
    video_id: Mapped[str | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL")
    )

    season: Mapped[Season] = relationship(back_populates="episodes")
    video: Mapped[Video | None] = relationship(lazy="joined")
