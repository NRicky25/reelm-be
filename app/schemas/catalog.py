"""API response shapes. These mirror reelm-fe/src/data/types.ts (camelCase).

Optional fields that are None are left out of responses (routes use
response_model_exclude_none), matching the frontend's optional `?:` fields.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.models import TitleType


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )


class EpisodeOut(CamelModel):
    id: str
    number: int
    name: str
    description: str
    duration_sec: int
    release_date: date
    thumb_url: str | None = None
    intro_start_sec: int | None = None
    intro_end_sec: int | None = None
    # Missing when the episode has no playable video yet.
    video_url: str | None = None


class SeasonOut(CamelModel):
    number: int
    episodes: list[EpisodeOut]


class TitleOut(CamelModel):
    id: str
    slug: str
    type: TitleType
    name: str
    description: str
    year: int
    genres: list[str]
    age_rating: str
    sub: bool | None = None
    dub: bool | None = None
    audio_languages: list[str]
    subtitle_languages: list[str]
    colors: list[str]
    poster_url: str | None = None
    backdrop_url: str | None = None
    poster_focus_x: int | None = None
    featured: bool
    staff_pick: bool
    added_at: date
    studio: str
    cast: list[str] = Field(validation_alias="cast_members")
    # Movies only
    duration_sec: int | None = None
    video_url: str | None = None
    intro_start_sec: int | None = None
    intro_end_sec: int | None = None
    # Anime & series only
    seasons: list[SeasonOut] | None = None


class NextEpisodeOut(CamelModel):
    season: int
    episode_id: str


class PlayableOut(CamelModel):
    title: TitleOut
    episode: EpisodeOut | None = None
    season: int | None = None
    video_url: str
    duration_sec: int
    intro_start_sec: int | None = None
    intro_end_sec: int | None = None
    next: NextEpisodeOut | None = None
