"""Admin screens for the catalogue.

List-type columns (languages, cast, colors) and a title's genres are edited as
simple comma-separated text instead of raw JSON / link tables.

Errors raised as ValueError in the hooks below are shown to the admin as a
message on the form (or the list page, for deletes).
"""

import re
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

import anyio
from sqladmin import ModelView
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request
from wtforms import Form, StringField, validators

from app.config import Settings
from app.models import Episode, Genre, Season, Title, TitleGenre, Video
from app.services.mux import MuxClient, MuxError

T = TypeVar("T")
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
DEFAULT_COLORS = ["#3a3a4a", "#22222b"]
GENRE_NAME_MAX = 60  # matches Genre.name length

OPTIONAL_SECONDS = [
    validators.Optional(),
    validators.NumberRange(min=0, message="Can't be negative."),
]


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def check_intro(data: dict) -> None:
    start, end = data.get("intro_start_sec"), data.get("intro_end_sec")
    if start == 0 and end == 0:  # "no intro" (as in the demo data)
        data["intro_start_sec"] = data["intro_end_sec"] = None
        return
    if (start is None) != (end is None):
        raise ValueError("Set both intro start and intro end, or neither.")
    if start is not None and end <= start:
        raise ValueError("Intro end must be after intro start.")


class BaseAdmin(ModelView):
    """Shared settings: no CSV/JSON export (nothing needs it; it makes bulk copying easy)."""

    can_export = False

    async def in_thread(self, fn: Callable[[Session], T]) -> T:
        """Run blocking database work in a worker thread with its own session."""

        def run() -> T:
            with self.session_maker() as session:
                return fn(session)

        return await anyio.to_thread.run_sync(run)


class CsvFieldsMixin:
    """Edit list columns as comma-separated text.

    `csv_fields` maps a form field name to (label, help text, model attribute).
    Subclasses must leave those attributes out of `form_columns`.
    """

    csv_fields: ClassVar[dict[str, tuple[str, str, str]]] = {}

    async def scaffold_form(self, rules: list[str] | None = None) -> type[Form]:
        base = await super().scaffold_form(rules)  # type: ignore[misc]
        extra = {
            name: StringField(
                label, description=help_text, render_kw={"class": "form-control"}
            )
            for name, (label, help_text, _attr) in self.csv_fields.items()
        }
        return type(f"{base.__name__}WithCsv", (base,), extra)

    async def get_form_data_for_edit(self, obj: Any) -> dict[str, Any]:
        data = await super().get_form_data_for_edit(obj)  # type: ignore[misc]
        for name, (_label, _help, attr) in self.csv_fields.items():
            data[name] = ", ".join(getattr(obj, attr) or [])
        return data

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        values: dict[str, list[str]] = {}
        for name, (_label, _help, attr) in self.csv_fields.items():
            raw = data.pop(name, None)
            if (
                raw is not None
            ):  # a field missing from the request leaves the value alone
                values[attr] = split_csv(raw)
        await super().on_model_change(data, model, is_created, request)  # type: ignore[misc]
        for attr, items in values.items():
            await self.set_csv_value(model, attr, items)

    async def set_csv_value(self, model: Any, attr: str, values: list[str]) -> None:
        setattr(model, attr, values)


def _video_ids_of_titles(session: Session, title_ids: list[str]) -> list[str]:
    movie = session.scalars(select(Title.video_id).where(Title.id.in_(title_ids))).all()
    episodes = session.scalars(
        select(Episode.video_id).join(Season).where(Season.title_id.in_(title_ids))
    ).all()
    return [v for v in (*movie, *episodes) if v]


_settings: Settings | None = None


def configure_views(settings: Settings) -> None:
    """Called by setup_admin: lets the views reach Mux when deleting videos."""
    global _settings
    _settings = settings


def _delete_videos(session: Session, video_ids: list[str]) -> None:
    # imported here to avoid a circular import
    from app.admin.upload import delete_videos_everywhere

    mux = (
        MuxClient.from_settings(_settings)
        if _settings and _settings.mux_enabled
        else None
    )
    try:
        delete_videos_everywhere(session, video_ids, mux)
    finally:
        if mux:
            mux.close()


class DeletesOwnVideosMixin:
    """Each episode/movie owns its video, so deleting it deletes the video too."""

    def video_ids_for(self, session: Session, model: Any) -> list[str]:
        raise NotImplementedError

    async def on_model_delete(self, model: Any, request: Request) -> None:
        await super().on_model_delete(model, request)  # type: ignore[misc]
        ids = await self.in_thread(lambda s: self.video_ids_for(s, model))  # type: ignore[attr-defined]
        request.state.videos_to_delete = ids

    async def after_model_delete(self, model: Any, request: Request) -> None:
        ids = getattr(request.state, "videos_to_delete", [])
        await self.in_thread(lambda s: _delete_videos(s, ids))  # type: ignore[attr-defined]
        await super().after_model_delete(model, request)  # type: ignore[misc]


# Only offer videos that nothing else uses yet (each video belongs to one episode/movie).
UNUSED_VIDEO = [
    ~exists().where(Episode.video_id == Video.id),
    ~exists().where(Title.video_id == Video.id),
]
VIDEO_AJAX = {
    "fields": ("url", "playback_id"),
    "order_by": "created_at",
    "limit": 20,
    "where": UNUSED_VIDEO,
}


class TitleAdmin(DeletesOwnVideosMixin, CsvFieldsMixin, BaseAdmin, model=Title):
    name = "Title"
    name_plural = "Titles"
    icon = "fa-solid fa-film"
    category = "Catalogue"

    column_list = [
        Title.name,
        Title.type,
        Title.year,
        Title.featured,
        Title.staff_pick,
        Title.added_at,
    ]
    column_searchable_list = [Title.name, Title.slug]
    column_sortable_list = [Title.name, Title.year, Title.added_at, Title.type]
    column_default_sort = [(Title.added_at, True)]
    page_size = 50

    form_columns = [
        Title.name,
        Title.slug,
        Title.type,
        Title.year,
        Title.description,
        Title.age_rating,
        Title.sub,
        Title.dub,
        Title.featured,
        Title.staff_pick,
        Title.added_at,
        Title.studio,
        Title.poster_url,
        Title.backdrop_url,
        Title.poster_focus_x,
        Title.duration_sec,
        Title.intro_start_sec,
        Title.intro_end_sec,
        Title.video,
    ]
    form_args: ClassVar = {
        "slug": {
            "description": "Used in the URL (/title/<slug>). Leave empty to make one from the name.",
            "validators": [validators.Optional()],
        },
        "year": {"validators": [validators.NumberRange(min=1880, max=2100)]},
        "sub": {"description": "Anime only: a subtitled version exists."},
        "dub": {"description": "Anime only: a dubbed version exists."},
        "poster_url": {
            "description": "Image URL or path, e.g. /images/stills/forest-bow.jpg"
        },
        "poster_focus_x": {
            "description": "0–100: where to centre when cropping a wide image into a tall poster.",
            "validators": [
                validators.Optional(),
                validators.NumberRange(min=0, max=100),
            ],
        },
        "duration_sec": {
            "description": "Movies only (seconds).",
            "validators": OPTIONAL_SECONDS,
        },
        "intro_start_sec": {
            "description": "Movies only: where the intro starts (seconds).",
            "validators": OPTIONAL_SECONDS,
        },
        "intro_end_sec": {
            "description": "Movies only: where the intro ends (seconds).",
            "validators": OPTIONAL_SECONDS,
        },
        "video": {"description": "Movies only. Shows use the video on each episode."},
    }
    form_ajax_refs: ClassVar = {"video": VIDEO_AJAX}
    csv_fields: ClassVar = {
        "genres_csv": (
            "Genres",
            "Comma-separated, in order of importance, e.g. Sci-Fi, Action",
            "genres",
        ),
        "audio_csv": (
            "Audio languages",
            "Comma-separated, e.g. Japanese, English",
            "audio_languages",
        ),
        "subtitles_csv": (
            "Subtitle languages",
            "Comma-separated",
            "subtitle_languages",
        ),
        "cast_csv": ("Cast", "Comma-separated names", "cast_members"),
        "colors_csv": (
            "Placeholder colours",
            "Two hex colours used when there's no image, e.g. #5a4a7a, #2a2b40. Empty = default.",
            "colors",
        ),
    }

    async def on_model_change(
        self, data: dict, model: Title, is_created: bool, request: Request
    ) -> None:
        check_intro(data)
        slug = slugify(data.get("slug") or "") or slugify(data.get("name") or "")
        if not slug:
            raise ValueError(
                "Couldn't make a URL slug from this name. Please type one (letters, numbers, dashes)."
            )
        taken = await self.in_thread(
            lambda s: s.scalar(
                select(Title.id).where(Title.slug == slug, Title.id != model.id)
            )
        )
        if taken:
            raise ValueError(
                f"The slug “{slug}” is already used by another title. Please choose another."
            )
        data["slug"] = slug
        await super().on_model_change(data, model, is_created, request)

    async def set_csv_value(self, model: Any, attr: str, values: list[str]) -> None:
        if attr == "colors":
            if not values:
                values = list(DEFAULT_COLORS)
            elif len(values) != 2 or not all(HEX_COLOR.match(v) for v in values):
                raise ValueError(
                    "Placeholder colours must be two hex colours, e.g. #5a4a7a, #2a2b40."
                )
        if attr == "genres":
            names = list(dict.fromkeys(values))  # keep order, drop duplicates
            if any(len(n) > GENRE_NAME_MAX for n in names):
                raise ValueError(
                    f"Genre names can be at most {GENRE_NAME_MAX} characters."
                )
            ids = await self._genre_ids(names)
            # Same transaction as the title: saved together or not at all.
            model.genre_links = [
                TitleGenre(genre_id=gid, position=i) for i, gid in enumerate(ids)
            ]
            return
        await super().set_csv_value(model, attr, values)

    async def _genre_ids(self, names: list[str]) -> list[int]:
        """IDs for these genre names, creating missing genres (an unused genre is harmless)."""

        def resolve(session: Session) -> list[int]:
            for attempt in range(2):
                existing = {g.name.lower(): g for g in session.scalars(select(Genre))}
                for name in names:
                    if name.lower() not in existing:
                        existing[name.lower()] = Genre(name=name)
                        session.add(existing[name.lower()])
                try:
                    session.commit()
                except (
                    IntegrityError
                ):  # someone created the same genre at the same moment
                    session.rollback()
                    if attempt:
                        raise
                    continue
                return [existing[n.lower()].id for n in names]
            raise AssertionError("unreachable")

        return await self.in_thread(resolve)

    def video_ids_for(self, session: Session, model: Title) -> list[str]:
        return _video_ids_of_titles(session, [model.id])


class SeasonAdmin(DeletesOwnVideosMixin, BaseAdmin, model=Season):
    name = "Season"
    name_plural = "Seasons"
    icon = "fa-solid fa-layer-group"
    category = "Catalogue"

    column_list = [Season.title, Season.number]
    column_sortable_list = [Season.number]
    form_columns = [Season.title, Season.number]
    form_args: ClassVar = {"number": {"validators": [validators.NumberRange(min=1)]}}
    form_ajax_refs: ClassVar = {
        "title": {"fields": ("name", "slug"), "order_by": "name"}
    }

    def video_ids_for(self, session: Session, model: Season) -> list[str]:
        return [
            v
            for v in session.scalars(
                select(Episode.video_id).where(Episode.season_id == model.id)
            )
            if v
        ]


class EpisodeAdmin(DeletesOwnVideosMixin, BaseAdmin, model=Episode):
    name = "Episode"
    name_plural = "Episodes"
    icon = "fa-solid fa-tv"
    category = "Catalogue"

    column_list = [
        Episode.season,
        Episode.number,
        Episode.name,
        Episode.release_date,
        Episode.video,
    ]
    column_searchable_list = [Episode.name, Episode.id]
    column_sortable_list = [Episode.number, Episode.release_date, Episode.name]
    column_default_sort = [(Episode.release_date, True)]
    page_size = 50
    form_columns = [
        Episode.season,
        Episode.number,
        Episode.name,
        Episode.description,
        Episode.release_date,
        Episode.duration_sec,
        Episode.thumb_url,
        Episode.intro_start_sec,
        Episode.intro_end_sec,
        Episode.video,
    ]
    form_args: ClassVar = {
        "number": {"validators": [validators.NumberRange(min=1)]},
        "release_date": {
            "description": "Before this date the episode shows as 'coming soon'."
        },
        "duration_sec": {
            "description": "Length in seconds (e.g. 1440 = 24 min).",
            "validators": [validators.NumberRange(min=0, message="Can't be negative.")],
        },
        "intro_start_sec": {
            "description": "Seconds; enables the Skip Intro button.",
            "validators": OPTIONAL_SECONDS,
        },
        "intro_end_sec": {"description": "Seconds.", "validators": OPTIONAL_SECONDS},
        "video": {"description": "Without a video the episode can't be played."},
    }
    form_ajax_refs: ClassVar = {"video": VIDEO_AJAX}

    async def on_model_change(
        self, data: dict, model: Episode, is_created: bool, request: Request
    ) -> None:
        check_intro(data)
        await super().on_model_change(data, model, is_created, request)

    def video_ids_for(self, session: Session, model: Episode) -> list[str]:
        vid = session.scalar(select(Episode.video_id).where(Episode.id == model.id))
        return [vid] if vid else []


class GenreAdmin(BaseAdmin, model=Genre):
    name = "Genre"
    name_plural = "Genres"
    icon = "fa-solid fa-tags"
    category = "Catalogue"

    column_list = [Genre.name]
    column_searchable_list = [Genre.name]
    column_sortable_list = [Genre.name]
    form_columns = [Genre.name]


class VideoAdmin(BaseAdmin, model=Video):
    name = "Video"
    name_plural = "Videos"
    icon = "fa-solid fa-circle-play"
    category = "Media"

    column_list = [
        Video.provider,
        Video.status,
        Video.playback_id,
        Video.url,
        Video.created_at,
    ]
    column_searchable_list = [Video.url, Video.playback_id, Video.asset_id]
    column_sortable_list = [Video.created_at, Video.status]
    column_default_sort = [(Video.created_at, True)]
    page_size = 50
    form_columns = [
        Video.provider,
        Video.status,
        Video.url,
        Video.playback_id,
        Video.duration_sec,
    ]
    form_args: ClassVar = {
        "url": {"description": "External videos: a direct .m3u8 or .mp4 link."},
        "playback_id": {
            "description": "Mux videos: filled in automatically after upload."
        },
        "duration_sec": {"validators": OPTIONAL_SECONDS},
    }

    async def on_model_delete(self, model: Video, request: Request) -> None:
        in_use = await self.in_thread(
            lambda s: s.scalar(
                select(
                    exists().where(Episode.video_id == model.id)
                    | exists().where(Title.video_id == model.id)
                )
            )
        )
        if in_use:
            raise ValueError(
                "This video is used by an episode or movie. Remove it there first."
            )
        await super().on_model_delete(model, request)

    async def after_model_delete(self, model: Video, request: Request) -> None:
        # The row is gone; remove the Mux asset too so it doesn't count toward the plan limit.
        if (
            model.provider.value == "mux"
            and model.asset_id
            and _settings
            and _settings.mux_enabled
        ):
            mux = MuxClient.from_settings(_settings)
            try:
                await anyio.to_thread.run_sync(mux.delete_asset, model.asset_id)
            except MuxError:
                pass  # best effort; it can also be removed in the Mux dashboard
            finally:
                mux.close()
        await super().after_model_delete(model, request)


ALL_VIEWS = [TitleAdmin, SeasonAdmin, EpisodeAdmin, GenreAdmin, VideoAdmin]
