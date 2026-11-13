"""Public, read-only catalogue endpoints used by reelm-fe."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_read_db
from app.models import TitleType
from app.schemas.catalog import PlayableOut, TitleOut, TitleSummaryOut
from app.services import catalog

router = APIRouter(tags=["catalogue"])
DB = Annotated[Session, Depends(get_read_db)]

# Omit None fields so responses match the frontend's optional (`?:`) fields.
OPTS = {"response_model_exclude_none": True, "response_model_by_alias": True}


@router.get("/titles", response_model=list[TitleOut] | list[TitleSummaryOut], **OPTS)
def list_titles(
    db: DB,
    type: TitleType | None = None,
    genre: Annotated[
        str | None, Query(description="Genre name or slug, e.g. slice-of-life")
    ] = None,
    list_name: Annotated[
        Literal["new", "recent", "az", "sub", "dub"] | None,
        Query(
            alias="list",
            description="new = recently released episodes, recent = recently added",
        ),
    ] = None,
    featured: bool | None = None,
    staff_pick: Annotated[bool | None, Query(alias="staffPick")] = None,
    slugs: Annotated[str | None, Query(description="Comma-separated slugs")] = None,
    limit: Annotated[int | None, Query(ge=1, le=200)] = None,
    full: Annotated[
        bool,
        Query(
            description="Include seasons + episodes. Only allowed together with `slugs`."
        ),
    ] = False,
):
    """Title summaries (no episode lists). Use /titles/{slug} for one full title."""
    if full and slugs is None:
        raise HTTPException(422, "`full=true` requires `slugs`")
    filters = catalog.TitleFilters(
        type=type,
        genre=genre,
        list_name=list_name,
        featured=featured,
        staff_pick=staff_pick,
        slugs=[s for s in slugs.split(",") if s] if slugs is not None else None,
        limit=limit,
    )
    summaries = catalog.list_titles(db, filters)
    if not full:
        return summaries
    by_slug = {t.slug: t for t in catalog.get_titles(db, [s.slug for s in summaries])}
    return [catalog.title_out(by_slug[s.slug]) for s in summaries if s.slug in by_slug]


@router.get("/titles/{slug}", response_model=TitleOut, **OPTS)
def get_title(slug: str, db: DB):
    t = catalog.get_title(db, slug)
    if t is None:
        raise HTTPException(404, "Title not found")
    return catalog.title_out(t)


@router.get("/titles/{slug}/related", response_model=list[TitleSummaryOut], **OPTS)
def related(slug: str, db: DB, limit: Annotated[int, Query(ge=1, le=50)] = 12):
    t = catalog.get_title(db, slug)
    if t is None:
        raise HTTPException(404, "Title not found")
    return catalog.related_titles(db, t, limit)


@router.get("/search", response_model=list[TitleSummaryOut], **OPTS)
def search(db: DB, q: Annotated[str, Query(max_length=100)] = ""):
    return catalog.search_titles(db, q)


@router.get("/genres", response_model=list[str])
def genres(db: DB, type: TitleType | None = None):
    return catalog.list_genres(db, type)


@router.get("/playback/{slug}", response_model=PlayableOut, **OPTS)
def playback(
    slug: str,
    db: DB,
    ep: str | None = None,
    s: Annotated[int | None, Query(ge=0, description="Season number")] = None,
    e: Annotated[int | None, Query(ge=0, description="Episode number")] = None,
):
    """An episode by id (`ep`) or by season + episode number (`s` + `e`)."""
    if (s is None) != (e is None):
        raise HTTPException(422, "Give both s (season) and e (episode)")
    p = catalog.playable(db, slug, ep, (s, e) if s is not None else None)
    if p is None:
        raise HTTPException(404, "Nothing playable here")
    return p
