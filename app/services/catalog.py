"""Catalogue queries and the conversion from database rows to API shapes.

Filtering (type, featured, staff pick, slugs, genre, search) happens in SQL.
List endpoints return summaries (no episode lists); only the detail and
playback endpoints load seasons and episodes.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, lazyload

from app.config import get_settings
from app.models import Episode, Genre, Season, Title, TitleGenre, TitleType, Video
from app.schemas.catalog import (
    EpisodeOut,
    LatestEpisodeOut,
    NextEpisodeOut,
    PlayableOut,
    SeasonOut,
    TitleOut,
    TitleSummaryOut,
)
from app.services.video_url import playable_clause, playback_url, subtitle_tracks

NEW_EPISODE_DAYS = 14  # a show is "new" if its latest episode is this recent
NEW_MOVIE_DAYS = 84  # a movie is "new" if it was added this recently


def today() -> date:
    """Catalogue date: CATALOG_TODAY if pinned, else today's UTC date (same as the frontend)."""
    return get_settings().catalog_today or datetime.now(UTC).date()


def genre_slug(name: str) -> str:
    return "-".join(name.lower().split())


# ───────── conversion ─────────
def episode_out(ep: Episode) -> EpisodeOut:
    out = EpisodeOut.model_validate(ep)
    out.video_url = playback_url(ep.video)
    return out


def summary_out(t: Title, latest: LatestEpisodeOut | None = None) -> TitleSummaryOut:
    out = TitleSummaryOut.model_validate(t)
    out.video_url = playback_url(t.video)
    out.latest_episode = latest
    return out


def title_out(t: Title) -> TitleOut:
    """Full title, including all seasons and episodes."""
    on = today()
    seasons = (
        None
        if t.type == TitleType.movie
        else [
            SeasonOut(number=s.number, episodes=[episode_out(e) for e in s.episodes])
            for s in t.seasons
        ]
    )
    latest = latest_available_episode(t, on)
    latest_out = (
        LatestEpisodeOut(
            season=latest[0],
            id=latest[1].id,
            number=latest[1].number,
            release_date=latest[1].release_date,
        )
        if latest
        else None
    )
    base = summary_out(t, latest_out).model_dump()
    return TitleOut(**base, seasons=seasons)


# ───────── availability ─────────
def is_available(ep: Episode, on: date) -> bool:
    """Released and has something to play."""
    return ep.release_date <= on and playback_url(ep.video) is not None


def latest_available_episode(t: Title, on: date) -> tuple[int, Episode] | None:
    """(season number, episode) of the newest playable episode; ties go to the later episode."""
    best: tuple[int, Episode] | None = None
    for s in t.seasons:
        for e in s.episodes:
            if is_available(e, on) and (
                best is None or e.release_date >= best[1].release_date
            ):
                best = (s.number, e)
    return best


def latest_episodes(
    db: Session, title_ids: Iterable[str], on: date
) -> dict[str, LatestEpisodeOut]:
    """Newest playable episode per title, in one query (no episode lists loaded)."""
    ids = list(title_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(
            Season.title_id,
            Season.number,
            Episode.id,
            Episode.number,
            Episode.release_date,
        )
        .join(Episode, Episode.season_id == Season.id)
        .join(Video, Episode.video_id == Video.id)
        .where(Season.title_id.in_(ids), Episode.release_date <= on, playable_clause())
        .order_by(Episode.release_date, Season.number, Episode.number)
    ).all()
    latest: dict[str, LatestEpisodeOut] = {}
    for (
        title_id,
        season_no,
        ep_id,
        ep_no,
        released,
    ) in rows:  # ascending → last one wins
        latest[title_id] = LatestEpisodeOut(
            season=season_no, id=ep_id, number=ep_no, release_date=released
        )
    return latest


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


def _titles_query(*, with_seasons: bool = False) -> Select[tuple[Title]]:
    stmt = select(Title).order_by(Title.added_at.desc(), Title.name)
    if not with_seasons:
        stmt = stmt.options(lazyload(Title.seasons))
    return stmt


def _genre_matches(genre: str):
    g = genre.lower()
    return Title.genre_links.any(
        TitleGenre.genre.has(
            or_(
                func.lower(Genre.name) == g,
                func.lower(func.replace(Genre.name, " ", "-")) == g,
            )
        )
    )


def list_titles(db: Session, f: TitleFilters) -> list[TitleSummaryOut]:
    stmt = _titles_query()
    if f.type:
        stmt = stmt.where(Title.type == f.type)
    if f.slugs is not None:
        stmt = stmt.where(Title.slug.in_(f.slugs))
    if f.featured is not None:
        stmt = stmt.where(Title.featured == f.featured)
    if f.staff_pick is not None:
        stmt = stmt.where(Title.staff_pick == f.staff_pick)
    if f.genre:
        stmt = stmt.where(_genre_matches(f.genre))
    if f.list_name == "az":
        stmt = stmt.order_by(None).order_by(func.lower(Title.name))
    elif f.list_name == "sub":
        stmt = stmt.where(Title.sub.is_(True))
    elif f.list_name == "dub":
        stmt = stmt.where(Title.dub.is_(True))
    # "recent" = default order (newest added first)

    titles = list(db.scalars(stmt).unique())
    on = today()
    latest = latest_episodes(
        db, (t.id for t in titles if t.type != TitleType.movie), on
    )

    if f.list_name == "new":
        fresh: list[tuple[date, Title]] = []
        for t in titles:
            if t.type == TitleType.movie:
                if t.added_at >= on - timedelta(days=NEW_MOVIE_DAYS):
                    fresh.append((t.added_at, t))
            elif (le := latest.get(t.id)) and (
                on - le.release_date
            ).days <= NEW_EPISODE_DAYS:
                fresh.append((le.release_date, t))
        titles = [t for _, t in sorted(fresh, key=lambda x: x[0], reverse=True)]

    if f.limit:
        titles = titles[: f.limit]
    return [summary_out(t, latest.get(t.id)) for t in titles]


def get_title(db: Session, slug: str) -> Title | None:
    return (
        db.scalars(_titles_query(with_seasons=True).where(Title.slug == slug))
        .unique()
        .first()
    )


def get_titles(db: Session, slugs: list[str]) -> list[Title]:
    """Several full titles in one go (a handful of queries, not a few per title)."""
    if not slugs:
        return []
    return list(
        db.scalars(
            _titles_query(with_seasons=True).where(Title.slug.in_(slugs))
        ).unique()
    )


def related_titles(db: Session, title: Title, limit: int = 12) -> list[TitleSummaryOut]:
    """Titles sharing genres, most overlap first; same type breaks ties."""
    mine = set(title.genres)
    candidates = db.scalars(
        _titles_query().where(
            Title.id != title.id,
            Title.genre_links.any(TitleGenre.genre.has(Genre.name.in_(mine))),
        )
    ).unique()
    scored = sorted(
        (
            (len(mine & set(t.genres)) + (0.5 if t.type == title.type else 0), t)
            for t in candidates
        ),
        key=lambda x: x[0],
        reverse=True,
    )[:limit]
    picked = [t for _, t in scored]
    latest = latest_episodes(
        db, (t.id for t in picked if t.type != TitleType.movie), today()
    )
    return [summary_out(t, latest.get(t.id)) for t in picked]


def search_titles(db: Session, q: str, limit: int = 60) -> list[TitleSummaryOut]:
    q = q.strip()
    if not q:
        return []
    stmt = _titles_query().where(
        or_(
            Title.name.icontains(q, autoescape=True),
            Title.genre_links.any(
                TitleGenre.genre.has(Genre.name.icontains(q, autoescape=True))
            ),
        )
    )
    ql = q.lower()
    found = sorted(
        db.scalars(stmt).unique(),
        key=lambda t: (not t.name.lower().startswith(ql), t.name.lower()),
    )
    found = found[:limit]
    latest = latest_episodes(
        db, (t.id for t in found if t.type != TitleType.movie), today()
    )
    return [summary_out(t, latest.get(t.id)) for t in found]


def list_genres(db: Session, title_type: TitleType | None = None) -> list[str]:
    stmt = select(Genre.name).join(TitleGenre).join(Title).distinct()
    if title_type:
        stmt = stmt.where(Title.type == title_type)
    return sorted(db.scalars(stmt), key=str.lower)


def playable(
    db: Session,
    slug: str,
    episode_id: str | None,
    season_episode: tuple[int, int] | None = None,
) -> PlayableOut | None:
    """What the player needs: the stream URL, intro/outro times and the next episode.

    The episode is picked by id, or by (season number, episode number), or else
    the first available one.
    """
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
            subtitles=subtitle_tracks(t.video),
            duration_sec=t.duration_sec or 0,
            intro_start_sec=t.intro_start_sec,
            intro_end_sec=t.intro_end_sec,
            outro_start_sec=t.outro_start_sec,
        )

    flat = [(s.number, e) for s in t.seasons for e in s.episodes]
    if episode_id:
        idx = next((i for i, (_, e) in enumerate(flat) if e.id == episode_id), None)
    elif season_episode:
        idx = next(
            (i for i, (s, e) in enumerate(flat) if (s, e.number) == season_episode),
            None,
        )
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
        subtitles=subtitle_tracks(ep.video),
        duration_sec=ep.duration_sec,
        intro_start_sec=ep.intro_start_sec,
        intro_end_sec=ep.intro_end_sec,
        outro_start_sec=ep.outro_start_sec,
        next=NextEpisodeOut(season=nxt[0], episode_id=nxt[1].id) if nxt else None,
    )
