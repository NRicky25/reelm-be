"""Load the demo catalogue (seed/catalogue.json, exported from reelm-fe) into the database.

The JSON uses the frontend's camelCase shapes (src/data/types.ts).
"""

import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    Episode,
    Genre,
    Season,
    Title,
    TitleGenre,
    TitleType,
    Video,
    VideoProvider,
    VideoStatus,
)

DEFAULT_SEED_FILE = Path(__file__).resolve().parent.parent / "seed" / "catalogue.json"


def load_catalogue(path: Path = DEFAULT_SEED_FILE) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def clear_catalogue(db: Session) -> None:
    for model in (Episode, Season, TitleGenre, Title, Genre, Video):
        db.execute(delete(model))


def seed(
    db: Session, titles: list[dict[str, Any]], *, reset: bool = False
) -> dict[str, int]:
    """Insert the catalogue. With reset=True, existing catalogue rows are removed first.

    Without reset, titles whose slug already exists are skipped (safe to run twice).
    """
    if reset:
        clear_catalogue(db)
        db.flush()

    genres: dict[str, Genre] = {g.name: g for g in db.scalars(select(Genre))}
    videos: dict[str, Video] = {v.url: v for v in db.scalars(select(Video)) if v.url}
    existing = set(db.scalars(select(Title.slug)))
    counts = {"titles": 0, "episodes": 0, "skipped": 0}

    def genre(name: str) -> Genre:
        if name not in genres:
            genres[name] = Genre(name=name)
            db.add(genres[name])
        return genres[name]

    def video(url: str | None) -> Video | None:
        if not url:
            return None
        if url not in videos:
            videos[url] = Video(
                provider=VideoProvider.external, url=url, status=VideoStatus.ready
            )
            db.add(videos[url])
        return videos[url]

    for t in titles:
        if t["slug"] in existing:
            counts["skipped"] += 1
            continue
        title = Title(
            id=t["id"],
            slug=t["slug"],
            type=TitleType(t["type"]),
            name=t["name"],
            description=t.get("description", ""),
            year=t["year"],
            age_rating=t.get("ageRating", "All"),
            sub=t.get("sub"),
            dub=t.get("dub"),
            audio_languages=t.get("audioLanguages", []),
            subtitle_languages=t.get("subtitleLanguages", []),
            colors=t.get("colors", ["#3a3a4a", "#22222b"]),
            poster_url=t.get("posterUrl"),
            backdrop_url=t.get("backdropUrl"),
            poster_focus_x=t.get("posterFocusX"),
            featured=bool(t.get("featured")),
            staff_pick=bool(t.get("staffPick")),
            added_at=date.fromisoformat(t["addedAt"]),
            studio=t.get("studio", ""),
            cast=t.get("cast", []),
            duration_sec=t.get("durationSec"),
            intro_start_sec=t.get("introStartSec"),
            intro_end_sec=t.get("introEndSec"),
            video=video(t.get("videoUrl")),
        )
        title.genre_links = [
            TitleGenre(genre=genre(g), position=i)
            for i, g in enumerate(t.get("genres", []))
        ]
        for s in t.get("seasons") or []:
            season = Season(number=s["number"])
            for e in s["episodes"]:
                season.episodes.append(
                    Episode(
                        id=e["id"],
                        number=e["number"],
                        name=e["name"],
                        description=e.get("description", ""),
                        duration_sec=e.get("durationSec", 0),
                        release_date=date.fromisoformat(e["releaseDate"]),
                        thumb_url=e.get("thumbUrl"),
                        intro_start_sec=e.get("introStartSec"),
                        intro_end_sec=e.get("introEndSec"),
                        video=video(e.get("videoUrl")),
                    )
                )
                counts["episodes"] += 1
            title.seasons.append(season)
        db.add(title)
        counts["titles"] += 1

    db.commit()
    return counts
