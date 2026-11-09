"""Admin panel: login protection and the custom form handling (genres, list fields)."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.admin.auth as auth_module
from app.admin import setup_admin
from app.config import Settings
from app.models import Episode, Season, Title

PASSWORD = "test-password-123"


@pytest.fixture
def admin_client(db: Session, monkeypatch) -> Iterator[TestClient]:
    async def no_wait(_seconds):  # skip the 1 s penalty on wrong passwords
        return None

    monkeypatch.setattr(auth_module.asyncio, "sleep", no_wait)
    api = FastAPI()
    settings = Settings(admin_password=PASSWORD, admin_secret_key="k" * 32)
    setup_admin(api, db.get_bind(), settings)
    with TestClient(api, base_url="http://testserver") as c:
        yield c


def login(client: TestClient, password: str = PASSWORD):
    return client.post(
        "/admin/login",
        data={"username": "admin", "password": password},
        follow_redirects=False,
    )


def test_admin_is_off_without_password(db):
    api = FastAPI()
    assert setup_admin(api, db.get_bind(), Settings(admin_password=None)) is None
    assert TestClient(api).get("/admin/").status_code == 404


def test_admin_requires_login(admin_client):
    r = admin_client.get("/admin/title/list", follow_redirects=False)
    assert r.status_code in (302, 303) and "/admin/login" in r.headers["location"]


def test_wrong_password_is_rejected(admin_client):
    r = login(admin_client, "nope")
    assert r.status_code == 400 or "/admin/login" in r.headers.get(
        "location", "/admin/login"
    )
    assert admin_client.get(
        "/admin/title/list", follow_redirects=False
    ).status_code in (302, 303)


def test_login_then_list_titles(admin_client):
    assert login(admin_client).status_code in (302, 303)
    page = admin_client.get("/admin/title/list")
    assert page.status_code == 200
    assert "The Last Lighthouse" in page.text
    for view in ("season", "episode", "genre", "video"):
        assert admin_client.get(f"/admin/{view}/list").status_code == 200, view


def test_create_title_with_genres_and_lists(admin_client, db):
    login(admin_client)
    r = admin_client.post(
        "/admin/title/create",
        data={
            "name": "Brand New Show!",
            "slug": "",
            "type": "series",
            "year": "2026",
            "description": "A test show.",
            "age_rating": "13+",
            "added_at": "2026-11-08",
            "studio": "Test Studio",
            "genres_csv": "Comedy, Brand New Genre, Comedy",
            "audio_csv": "English, Japanese",
            "subtitles_csv": "",
            "cast_csv": "A. Person,  B. Person ",
            "colors_csv": "#111111, #222222",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:500]
    db.expire_all()
    t = db.scalar(select(Title).where(Title.slug == "brand-new-show"))
    assert t is not None, "slug is generated from the name"
    assert t.genres == ["Comedy", "Brand New Genre"]  # order kept, duplicate dropped
    assert t.audio_languages == ["English", "Japanese"]
    assert t.subtitle_languages == []
    assert t.cast_members == ["A. Person", "B. Person"]
    assert t.colors == ["#111111", "#222222"]
    assert t.id  # generated


def test_edit_form_shows_and_updates_genres(admin_client, db):
    login(admin_client)
    t = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    form = admin_client.get(f"/admin/title/edit/{t.id}")
    assert form.status_code == 200
    assert "Sci-Fi, Action, Drama" in form.text
    assert "Elena" not in form.text  # (sanity: another title's cast isn't shown)

    data = {
        "name": t.name,
        "slug": t.slug,
        "type": t.type.value,
        "year": str(t.year),
        "description": t.description,
        "age_rating": t.age_rating,
        "added_at": t.added_at.isoformat(),
        "studio": t.studio,
        "featured": "y",
        "genres_csv": "Drama, Sci-Fi",
        "audio_csv": ", ".join(t.audio_languages),
        "subtitles_csv": ", ".join(t.subtitle_languages),
        "cast_csv": ", ".join(t.cast_members),
        "colors_csv": ", ".join(t.colors),
    }
    r = admin_client.post(
        f"/admin/title/edit/{t.id}", data=data, follow_redirects=False
    )
    assert r.status_code in (302, 303), r.text[:500]
    db.expire_all()
    t = db.get(Title, t.id)
    assert t.genres == ["Drama", "Sci-Fi"]
    assert len(t.seasons) == 2, "editing a title must not touch its seasons"


def test_create_episode(admin_client, db):
    login(admin_client)
    season = db.scalar(select(Season).join(Title).where(Title.slug == "hollow-tide"))
    r = admin_client.post(
        "/admin/episode/create",
        data={
            "season": str(season.id),
            "number": "99",
            "name": "Admin Made This",
            "description": "",
            "release_date": "2026-11-01",
            "duration_sec": "1440",
        },
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:500]
    db.expire_all()
    ep = db.scalar(select(Episode).where(Episode.name == "Admin Made This"))
    assert ep is not None and ep.season_id == season.id and ep.id
