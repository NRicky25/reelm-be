"""Catalogue queries and the conversion from database rows to API shapes.

The catalogue is small, so list filtering like "new episodes" happens in
Python after one query. Move it into SQL if the catalogue grows large.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Episode, Genre, Title, TitleGenre, TitleType
from app.schemas.catalog import (
    EpisodeOut,
    NextEpisodeOut,
    PlayableOut,
    SeasonOut,
    TitleOut,
)
from app.services.video_url import playback_url

NEW_EPISODE_DAYS = 14


def today() -> date:
    return get_settings().catalog_today or date.today()


def genre_slug(name: str) -> str:
    return name.lower().replace(" ", "-")


# ───────── conversion ─────────
def episode_out(ep: Episode) -> EpisodeOut:
    out = EpisodeOut.model_validate(ep)
    out.video_url = playback_url(ep.video)
    return out


def title_out(t: Title) -> TitleOut:
    out = TitleOut.model_validate(t)
    out.video_url = playback_url(t.video)
    if t.type == TitleType.movie:
        out.seasons = None
    else:
        out.seasons = [
            SeasonOut(number=s.number, episodes=[episode_out(e) for e in s.episodes])
            for s in t.seasons
        ]
    return out


# ───────── helpers ─────────
def is_available(ep: Episode, on: date) -> bool:
    """Released and has something to play."""
    return ep.release_date <= on and playback_url(ep.video) is not None


def latest_available_episode(t: Title, on: date) -> Episode | None:
    eps = [e for s in t.seasons for e in s.episodes if is_available(e, on)]
    return max(eps, key=lambda e: e.release_date, default=None)


def _all_titles(db: Session, title_type: TitleType | None = None) -> list[Title]:
    stmt = select(Title).order_by(Title.added_at.desc(), Title.name)
    if title_type:
        stmt = stmt.where(Title.type == title_type)
    return list(db.scalars(stmt).unique())


# ───────── queries ─────────
@dataclass
class TitleFilters:
    type: TitleType | None = None
    genre: str | None = None  # genre name or slug
    list_name: str | None = None  # new | recent | az | sub | dub
    featured: bool | None = None
    staff_pick: bool | None = None
    slugs: list[str] | None = None
    limit: int | None = None


LISTS = {"new", "recent", "az", "sub", "dub"}


def list_titles(db: Session, f: TitleFilters) -> list[Title]:
    titles = _all_titles(db, f.type)

    if f.slugs is not None:
        wanted = set(f.slugs)
        titles = [t for t in titles if t.slug in wanted]
    if f.featured is not None:
        titles = [t for t in titles if t.featured == f.featured]
    if f.staff_pick is not None:
        titles = [t for t in titles if t.staff_pick == f.staff_pick]
    if f.genre:
        g = f.genre.lower()
        titles = [
            t for t in titles if any(g in (n.lower(), genre_slug(n)) for n in t.genres)
        ]

    if f.list_name == "new":
        on = today()
        if f.type == TitleType.movie:
            titles = [
                t
                for t in titles
                if t.added_at >= on - timedelta(days=NEW_EPISODE_DAYS * 6)
            ]
        else:
            fresh = []
            for t in titles:
                latest = latest_available_episode(t, on)
                if latest and (on - latest.release_date).days <= NEW_EPISODE_DAYS:
                    fresh.append((latest.release_date, t))
            titles = [t for _, t in sorted(fresh, key=lambda x: x[0], reverse=True)]
    elif f.list_name == "az":
        titles = sorted(titles, key=lambda t: t.name.lower())
    elif f.list_name == "sub":
        titles = [t for t in titles if t.sub]
    elif f.list_name == "dub":
        titles = [t for t in titles if t.dub]
    # "recent" = default order (newest added first)

    return titles[: f.limit] if f.limit else titles


def get_title(db: Session, slug: str) -> Title | None:
    return db.scalars(select(Title).where(Title.slug == slug)).unique().first()


def related_titles(db: Session, title: Title, limit: int = 12) -> list[Title]:
    """Titles sharing genres, most overlap first; same type breaks ties."""
    mine = set(title.genres)
    scored = []
    for t in _all_titles(db):
        if t.id == title.id:
            continue
        shared = len(mine & set(t.genres))
        if shared:
            scored.append((shared + (0.5 if t.type == title.type else 0), t))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:limit]]


def search_titles(db: Session, q: str, limit: int = 60) -> list[Title]:
    q = q.strip()
    if not q:
        return []
    like = f"%{q.lower()}%"
    stmt = (
        select(Title)
        .outerjoin(TitleGenre)
        .outerjoin(Genre)
        .where(or_(Title.name.ilike(like), Genre.name.ilike(like)))
    )
    found = {t.id: t for t in db.scalars(stmt).unique()}.values()
    ql = q.lower()
    return sorted(
        found, key=lambda t: (not t.name.lower().startswith(ql), t.name.lower())
    )[:limit]


def list_genres(db: Session, title_type: TitleType | None = None) -> list[str]:
    stmt = (
        select(Genre.name).join(TitleGenre).join(Title).distinct().order_by(Genre.name)
    )
    if title_type:
        stmt = stmt.where(Title.type == title_type)
    return list(db.scalars(stmt))


def playable(db: Session, slug: str, episode_id: str | None) -> PlayableOut | None:
    """What the player needs: the stream URL, intro times and the next episode."""
    t = get_title(db, slug)
    if t is None:
        return None
    on = today()

    if t.type == TitleType.movie:
        url = playback_url(t.video)
        if not url:
            return None
        return PlayableOut(
            title=title_out(t),
            video_url=url,
            duration_sec=t.duration_sec or 0,
            intro_start_sec=t.intro_start_sec,
            intro_end_sec=t.intro_end_sec,
        )

    flat = [(s.number, e) for s in t.seasons for e in s.episodes]
    if episode_id:
        idx = next((i for i, (_, e) in enumerate(flat) if e.id == episode_id), None)
    else:
        idx = next((i for i, (_, e) in enumerate(flat) if is_available(e, on)), None)
    if idx is None or not is_available(flat[idx][1], on):
        return None

    season_no, ep = flat[idx]
    nxt = next(((s, e) for s, e in flat[idx + 1 :] if is_available(e, on)), None)
    return PlayableOut(
        title=title_out(t),
        episode=episode_out(ep),
        season=season_no,
        video_url=playback_url(ep.video),
        duration_sec=ep.duration_sec,
        intro_start_sec=ep.intro_start_sec,
        intro_end_sec=ep.intro_end_sec,
        next=NextEpisodeOut(season=nxt[0], episode_id=nxt[1].id) if nxt else None,
    )
