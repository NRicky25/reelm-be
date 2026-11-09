"""Admin screens for the catalogue.

List-type columns (languages, cast, colors) and a title's genres are edited as
simple comma-separated text instead of raw JSON / link tables.
"""

import re
from typing import Any, ClassVar

from sqladmin import ModelView
from sqlalchemy import select
from starlette.requests import Request
from wtforms import Form, StringField, validators

from app.models import Episode, Genre, Season, Title, TitleGenre, Video


def split_csv(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


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
            data[name] = ", ".join(self.csv_value(obj, attr))
        return data

    def csv_value(self, obj: Any, attr: str) -> list[str]:
        return list(getattr(obj, attr) or [])

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        await super().on_model_change(data, model, is_created, request)  # type: ignore[misc]
        for name, (_label, _help, attr) in self.csv_fields.items():
            if name in data:
                self.set_csv_value(model, attr, split_csv(data.pop(name)), request)

    def set_csv_value(
        self, model: Any, attr: str, values: list[str], request: Request
    ) -> None:
        setattr(model, attr, values)


class TitleAdmin(CsvFieldsMixin, ModelView, model=Title):
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
        "sub": {"description": "Anime only: a subtitled version exists."},
        "dub": {"description": "Anime only: a dubbed version exists."},
        "poster_url": {
            "description": "Image URL or path, e.g. /images/stills/forest-bow.jpg"
        },
        "poster_focus_x": {
            "description": "0–100: where to centre when cropping a wide image into a tall poster."
        },
        "duration_sec": {"description": "Movies only (seconds)."},
        "intro_start_sec": {
            "description": "Movies only: where the intro starts (seconds)."
        },
        "intro_end_sec": {
            "description": "Movies only: where the intro ends (seconds)."
        },
        "video": {"description": "Movies only. Shows use the video on each episode."},
    }
    form_ajax_refs: ClassVar = {
        "video": {
            "fields": ("url", "playback_id"),
            "order_by": "created_at",
            "page_size": 20,
        }
    }
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
            "Two hex colours used when there's no image, e.g. #5a4a7a, #2a2b40",
            "colors",
        ),
    }

    async def on_model_change(
        self, data: dict, model: Title, is_created: bool, request: Request
    ) -> None:
        if not (data.get("slug") or "").strip():
            data["slug"] = slugify(data.get("name") or "")
        await super().on_model_change(data, model, is_created, request)

    def set_csv_value(
        self, model: Any, attr: str, values: list[str], request: Request
    ) -> None:
        if attr == "genres":
            # Genre links need a database session and the saved title id, so
            # they're applied in after_model_change.
            request.state.pending_genres = values
            return
        super().set_csv_value(model, attr, values, request)

    async def after_model_change(
        self, data: dict, model: Title, is_created: bool, request: Request
    ):
        names = getattr(request.state, "pending_genres", None)
        if names is not None:
            del request.state.pending_genres
            self._save_genres(model.id, names)
        return await super().after_model_change(data, model, is_created, request)

    def _save_genres(self, title_id: str, names: list[str]) -> None:
        unique = list(dict.fromkeys(names))  # keep order, drop duplicates
        with self.session_maker() as session:
            title = session.get(Title, title_id)
            existing = {g.name.lower(): g for g in session.scalars(select(Genre))}
            # Remove old links first: new ones may reuse the same (title, genre) keys.
            title.genre_links.clear()
            session.flush()
            for i, name in enumerate(unique):
                genre = existing.get(name.lower())
                if genre is None:
                    genre = existing[name.lower()] = Genre(name=name)
                    session.add(genre)
                link = TitleGenre(position=i)
                title.genre_links.append(link)  # in the session via the title first...
                link.genre = genre  # ...then linked to its genre
            session.commit()


class SeasonAdmin(ModelView, model=Season):
    name = "Season"
    name_plural = "Seasons"
    icon = "fa-solid fa-layer-group"
    category = "Catalogue"

    column_list = [Season.title, Season.number]
    column_sortable_list = [Season.number]
    form_columns = [Season.title, Season.number]
    form_ajax_refs: ClassVar = {
        "title": {"fields": ("name", "slug"), "order_by": "name"}
    }


class EpisodeAdmin(ModelView, model=Episode):
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
        "release_date": {
            "description": "Before this date the episode shows as 'coming soon'."
        },
        "duration_sec": {"description": "Length in seconds (e.g. 1440 = 24 min)."},
        "intro_start_sec": {"description": "Seconds; enables the Skip Intro button."},
        "intro_end_sec": {"description": "Seconds."},
        "video": {"description": "Without a video the episode can't be played."},
    }
    form_ajax_refs: ClassVar = {
        "video": {
            "fields": ("url", "playback_id"),
            "order_by": "created_at",
            "page_size": 20,
        },
    }


class GenreAdmin(ModelView, model=Genre):
    name = "Genre"
    name_plural = "Genres"
    icon = "fa-solid fa-tags"
    category = "Catalogue"

    column_list = [Genre.name]
    column_searchable_list = [Genre.name]
    column_sortable_list = [Genre.name]
    form_columns = [Genre.name]


class VideoAdmin(ModelView, model=Video):
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
    }


ALL_VIEWS = [TitleAdmin, SeasonAdmin, EpisodeAdmin, GenreAdmin, VideoAdmin]
