"""Admin screens for the catalogue.

List-type columns (languages, cast, colors) and a title's genres are edited as
simple comma-separated text instead of raw JSON / link tables.

Errors raised as ValueError in the hooks below are shown to the admin as a
message on the form (or the list page, for deletes).
"""

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, TypeVar

import anyio
from sqladmin import ModelView
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request
from wtforms import FileField, FloatField, Form, StringField, validators

from app.config import Settings
from app.models import Episode, Genre, Season, Title, TitleGenre, Video
from app.services.storage import (
    MAX_BYTES,
    MediaStorage,
    StorageError,
    public_prefix,
)

log = logging.getLogger(__name__)
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


def check_outro(data: dict) -> None:
    """The end credits start after the intro, and before the end of the video."""
    outro = data.get("outro_start_sec")
    if not outro:  # empty, or 0: no outro
        data["outro_start_sec"] = None
        return
    intro_end = data.get("intro_end_sec")
    if intro_end is not None and outro < intro_end:
        raise ValueError("Credits start must be after the intro ends.")
    duration = data.get("duration_sec")
    if duration and outro >= duration:
        raise ValueError(
            f"Credits start ({outro} s) must be before the end of the video ({duration} s)."
        )


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
    """Called by setup_admin: lets the views reach image storage."""
    global _settings
    _settings = settings


def _delete_videos(session: Session, video_ids: list[str]) -> None:
    """Delete the video rows (the files on R2 stay; remove them in the R2 dashboard)."""
    if not video_ids:
        return
    for video in session.scalars(select(Video).where(Video.id.in_(video_ids))):
        session.delete(video)
    session.commit()


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
    "fields": ("url",),
    "order_by": "created_at",
    "limit": 20,
    "where": UNUSED_VIDEO,
}


def _image_urls_in_use(session: Session, urls: list[str]) -> set[str]:
    """Which of these URLs a title or episode still shows."""
    used: set[str] = set()
    for column in (Title.poster_url, Title.backdrop_url, Episode.thumb_url):
        used.update(session.scalars(select(column).where(column.in_(urls))))
    return used


def delete_unused_images(session: Session, urls: list[str]) -> None:
    """Best effort: remove uploaded images that no title/episode uses any more.

    Images outside our media folder (e.g. /images/... paths) are never touched, and an
    image still used by another row (shared demo stills, a reused URL) is kept.
    """
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls or not (_settings and _settings.storage_enabled):
        return
    storage = MediaStorage.from_settings(_settings)
    try:
        ours = {u: p for u in urls if (p := storage.path_from_url(u))}
        if not ours:
            return
        used = _image_urls_in_use(session, list(ours))
        storage.delete([p for u, p in ours.items() if u not in used])
    except StorageError:
        log.warning("Couldn't delete unused images %s", urls, exc_info=True)
    finally:
        storage.close()


VIDEO_URL = re.compile(r"^https?://[^\s/]+/\S*\.(m3u8|mp4)(\?\S*)?$", re.IGNORECASE)


def check_video_url(url: str | None) -> str:
    """A video link must be a full http(s) link to an .m3u8 playlist or an .mp4 file."""
    url = (url or "").strip()
    if not VIDEO_URL.match(url):
        raise ValueError(
            "Link: paste the full link to the video's master.m3u8 (or an .mp4 file), "
            "e.g. https://pub-xxxx.r2.dev/videos/episode-1-3f9a2c/master.m3u8"
        )
    return url


def check_image_url(label: str, url: str | None) -> None:
    """Image URLs must be a site path (/images/...) or point into our media folder.

    The frontend only loads images from those places; anything else would break
    every page that shows the title.
    """
    if not url:
        return
    if url.startswith("/") and not url.startswith("//"):
        return
    if (
        _settings
        and _settings.media_public_url
        and url.startswith(public_prefix(_settings.media_public_url))
    ):
        return
    raise ValueError(
        f"{label}: upload a file, or use a path like /images/stills/forest-bow.jpg. "
        "Images from other websites can't be shown."
    )


class ImageUploadMixin:
    """Adds "Upload …" file fields that save images into the media folder.

    `image_fields` maps a form field to (label, model URL attribute, storage folder).
    The URL fields stay editable too (a site path or a URL from our media folder).

    Files are uploaded *before* the database save (so no DB connection waits on
    the upload) and removed again if the save fails. Replaced images are deleted
    after a successful save, unless another row still uses them.
    """

    image_fields: ClassVar[dict[str, tuple[str, str, str]]] = {}

    async def scaffold_form(self, rules: list[str] | None = None) -> type[Form]:
        base = await super().scaffold_form(rules)  # type: ignore[misc]
        extra = {
            name: FileField(
                label,
                description="JPEG, PNG or WebP, up to 8 MB. Replaces the image above.",
                render_kw={
                    "class": "form-control",
                    "accept": "image/jpeg,image/png,image/webp",
                },
            )
            for name, (label, _attr, _folder) in self.image_fields.items()
        }
        return type(f"{base.__name__}WithImages", (base,), extra)

    async def _upload_files(self, data: dict) -> list[str]:
        """Upload chosen files and put their URLs into `data`. Returns the new URLs."""
        files: list[tuple[str, str, bytes]] = []
        for name, (_label, attr, folder) in self.image_fields.items():
            file = data.pop(name, None)
            if file is not None and getattr(file, "filename", None):
                content = await file.read(MAX_BYTES + 1)  # enough to spot "too large"
                if content:
                    files.append((attr, folder, content))
        if not files:
            return []
        if not (_settings and _settings.storage_enabled):
            raise ValueError(
                "Image uploads need MEDIA_ROOT and MEDIA_PUBLIC_URL in .env."
            )

        def upload_all() -> list[tuple[str, str]]:
            storage = MediaStorage.from_settings(_settings)
            done: list[tuple[str, str]] = []
            try:
                for attr, folder, content in files:
                    done.append((attr, storage.upload(folder, content)))
                return done
            except StorageError:
                # don't leave the first image behind if the second one fails
                paths = [storage.path_from_url(url) for _, url in done]
                try:
                    storage.delete([p for p in paths if p])
                except StorageError:
                    pass
                raise
            finally:
                storage.close()

        try:
            results = await anyio.to_thread.run_sync(upload_all)
        except StorageError as e:
            raise ValueError(str(e)) from e
        for attr, url in results:
            data[attr] = (
                url  # SQLAdmin applies `data` to the model after on_model_change
            )
        return [url for _, url in results]

    async def _save_with_uploads(
        self, data: dict, save: Callable[[], Awaitable[Any]]
    ) -> Any:
        new_urls = await self._upload_files(data)
        try:
            return await save()
        except BaseException:
            if new_urls:  # the save failed: nothing uses these images
                await self.in_thread(lambda s: delete_unused_images(s, new_urls))  # type: ignore[attr-defined]
            raise

    async def insert_model(self, request: Request, data: dict) -> Any:
        parent = super()
        return await self._save_with_uploads(
            data,
            lambda: parent.insert_model(request, data),  # type: ignore[attr-defined]
        )

    async def update_model(self, request: Request, pk: str, data: dict) -> Any:
        parent = super()
        return await self._save_with_uploads(
            data,
            lambda: parent.update_model(request, pk, data),  # type: ignore[attr-defined]
        )

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        replaced = []
        for label, attr, _folder in self.image_fields.values():
            if attr not in data:
                continue
            check_image_url(label.removeprefix("Upload ").capitalize(), data[attr])
            old = None if is_created else getattr(model, attr, None)
            if old and old != data[attr]:
                replaced.append(old)
        await super().on_model_change(data, model, is_created, request)  # type: ignore[misc]
        request.state.replaced_images = replaced

    async def after_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ):
        old_urls = getattr(request.state, "replaced_images", [])
        if old_urls:
            await self.in_thread(lambda s: delete_unused_images(s, old_urls))  # type: ignore[attr-defined]
        return await super().after_model_change(data, model, is_created, request)  # type: ignore[misc]


class DeletesOwnImagesMixin:
    """Deleting a title/season/episode removes its uploaded images (if unused elsewhere)."""

    def image_urls_for(self, session: Session, model: Any) -> list[str]:
        raise NotImplementedError

    async def on_model_delete(self, model: Any, request: Request) -> None:
        await super().on_model_delete(model, request)  # type: ignore[misc]
        request.state.images_to_delete = await self.in_thread(  # type: ignore[attr-defined]
            lambda s: self.image_urls_for(s, model)
        )

    async def after_model_delete(self, model: Any, request: Request) -> None:
        urls = getattr(request.state, "images_to_delete", [])
        if urls:
            await self.in_thread(lambda s: delete_unused_images(s, urls))  # type: ignore[attr-defined]
        await super().after_model_delete(model, request)  # type: ignore[misc]


def _episode_thumbs(session: Session, *where: Any) -> list[str]:
    return [u for u in session.scalars(select(Episode.thumb_url).where(*where)) if u]


class TitleAdmin(
    DeletesOwnImagesMixin,
    DeletesOwnVideosMixin,
    ImageUploadMixin,
    CsvFieldsMixin,
    BaseAdmin,
    model=Title,
):
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
        Title.outro_start_sec,
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
            "description": "Filled in by the upload below, or a path like /images/stills/forest-bow.jpg"
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
        "outro_start_sec": {
            "label": "Credits start (sec)",
            "description": "Movies only: where the end credits start (seconds); they run to the end. "
            "Enables the Skip Credits button.",
            "validators": OPTIONAL_SECONDS,
        },
        "video": {"description": "Movies only. Shows use the video on each episode."},
    }
    form_ajax_refs: ClassVar = {"video": VIDEO_AJAX}
    image_fields: ClassVar = {
        "poster_file": ("Upload poster", "poster_url", "posters"),
        "backdrop_file": ("Upload backdrop", "backdrop_url", "backdrops"),
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
            "Two hex colours used when there's no image, e.g. #5a4a7a, #2a2b40. Empty = default.",
            "colors",
        ),
    }

    async def on_model_change(
        self, data: dict, model: Title, is_created: bool, request: Request
    ) -> None:
        check_intro(data)
        check_outro(data)
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

    def image_urls_for(self, session: Session, model: Title) -> list[str]:
        own = session.execute(
            select(Title.poster_url, Title.backdrop_url).where(Title.id == model.id)
        ).one_or_none()
        seasons = select(Season.id).where(Season.title_id == model.id)
        thumbs = _episode_thumbs(session, Episode.season_id.in_(seasons))
        return [u for u in (*(own or ()), *thumbs) if u]


class SeasonAdmin(
    DeletesOwnImagesMixin, DeletesOwnVideosMixin, BaseAdmin, model=Season
):
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
        ids = session.scalars(
            select(Episode.video_id).where(Episode.season_id == model.id)
        ).all()
        return [v for v in ids if v]

    def image_urls_for(self, session: Session, model: Season) -> list[str]:
        return _episode_thumbs(session, Episode.season_id == model.id)


class EpisodeAdmin(
    DeletesOwnImagesMixin,
    DeletesOwnVideosMixin,
    ImageUploadMixin,
    BaseAdmin,
    model=Episode,
):
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
        Episode.outro_start_sec,
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
        "outro_start_sec": {
            "label": "Credits start (sec)",
            "description": "Where the end credits start (seconds); they run to the end. "
            "Enables the Next Episode button there.",
            "validators": OPTIONAL_SECONDS,
        },
        "video": {"description": "Without a video the episode can't be played."},
    }
    image_fields: ClassVar = {
        "thumb_file": ("Upload thumbnail", "thumb_url", "thumbnails")
    }
    form_ajax_refs: ClassVar = {"video": VIDEO_AJAX}

    async def on_model_change(
        self, data: dict, model: Episode, is_created: bool, request: Request
    ) -> None:
        check_intro(data)
        check_outro(data)
        await super().on_model_change(data, model, is_created, request)

    def video_ids_for(self, session: Session, model: Episode) -> list[str]:
        vid = session.scalar(select(Episode.video_id).where(Episode.id == model.id))
        return [vid] if vid else []

    def image_urls_for(self, session: Session, model: Episode) -> list[str]:
        return _episode_thumbs(session, Episode.id == model.id)


class GenreAdmin(BaseAdmin, model=Genre):
    name = "Genre"
    name_plural = "Genres"
    icon = "fa-solid fa-tags"
    category = "Catalogue"

    column_list = [Genre.name]
    column_searchable_list = [Genre.name]
    column_sortable_list = [Genre.name]
    form_columns = [Genre.name]


LANG_CODE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


class VideoAdmin(CsvFieldsMixin, BaseAdmin, model=Video):
    name = "Video"
    name_plural = "Videos"
    icon = "fa-solid fa-circle-play"
    category = "Media"

    column_list = [Video.url, Video.duration_sec, Video.subtitles, Video.created_at]
    column_labels: ClassVar = {"url": "Link", "duration_sec": "Duration (s)"}
    # a video left over from Mux has no link: say what it is instead of a blank cell
    column_formatters: ClassVar = {Video.url: lambda m, _a: str(m)}
    column_searchable_list = [Video.url]
    column_sortable_list = [Video.created_at]
    column_default_sort = [(Video.created_at, True)]
    page_size = 50
    form_columns = [Video.url, Video.duration_sec]
    # SQLAdmin has no built-in form field for Float columns (the form would crash)
    form_overrides: ClassVar = {"duration_sec": FloatField}
    form_args: ClassVar = {
        "url": {
            "label": "Link",
            "description": (
                "The video's master.m3u8 on R2, e.g. "
                "https://pub-xxxx.r2.dev/videos/episode-1-3f9a2c/master.m3u8 "
                "(Reelm Encoder shows it). A direct .mp4 link works too."
            ),
            "validators": [validators.InputRequired()],
        },
        "duration_sec": {
            "label": "Duration (seconds)",
            "description": "Reelm Encoder shows it, with a Copy button.",
            "validators": OPTIONAL_SECONDS,
        },
    }

    csv_fields: ClassVar = {
        "subtitles_csv": (
            "Subtitles",
            (
                "Language codes of the subtitle files next to master.m3u8, e.g. en "
                "(for en.vtt), or en, es. R2 Uploader shows them. Empty = none."
            ),
            "subtitles",
        )
    }

    async def on_model_change(
        self, data: dict, model: Video, is_created: bool, request: Request
    ) -> None:
        if "url" in data:
            data["url"] = check_video_url(data.get("url"))
        await super().on_model_change(data, model, is_created, request)

    async def set_csv_value(self, model: Any, attr: str, values: list[str]) -> None:
        if attr == "subtitles":
            codes = list(dict.fromkeys(v.strip().lower() for v in values))
            bad = [c for c in codes if not LANG_CODE.match(c)]
            if bad:
                raise ValueError(
                    f"Subtitles: “{bad[0]}” isn't a language code. Use the file's name "
                    "without .vtt, e.g. en for en.vtt (or en, es)."
                )
            values = codes
        await super().set_csv_value(model, attr, values)

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


ALL_VIEWS = [TitleAdmin, SeasonAdmin, EpisodeAdmin, GenreAdmin, VideoAdmin]
