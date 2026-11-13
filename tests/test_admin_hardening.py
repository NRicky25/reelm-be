"""Admin panel: security and data-safety checks (step 3 review follow-ups)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.admin import setup_admin
from app.admin.auth import MAX_FAILURES, AdminAuth
from app.admin.views import TitleAdmin
from app.config import Settings
from app.models import Episode, Title, Video, VideoProvider
from tests.test_admin import PASSWORD, admin_client, login  # noqa: F401  (fixture)


def title_form(t: Title, **overrides) -> dict:
    """The form exactly as the admin's edit page would submit it, plus overrides."""
    data = {
        "name": t.name,
        "slug": t.slug,
        "type": t.type.value,
        "year": str(t.year),
        "description": t.description,
        "age_rating": t.age_rating,
        "added_at": t.added_at.isoformat(),
        "studio": t.studio,
        "poster_url": t.poster_url or "",
        "backdrop_url": t.backdrop_url or "",
        "poster_focus_x": "" if t.poster_focus_x is None else str(t.poster_focus_x),
        "genres_csv": ", ".join(t.genres),
        "audio_csv": ", ".join(t.audio_languages),
        "subtitles_csv": ", ".join(t.subtitle_languages),
        "cast_csv": ", ".join(t.cast_members),
        "colors_csv": ", ".join(t.colors),
    }
    if t.sub is not None:
        data["sub"] = str(t.sub)
    if t.dub is not None:
        data["dub"] = str(t.dub)
    if t.featured:
        data["featured"] = "y"
    if t.staff_pick:
        data["staff_pick"] = "y"
    if t.video_id:
        data["video"] = t.video_id
    for key in ("duration_sec", "intro_start_sec", "intro_end_sec", "outro_start_sec"):
        value = getattr(t, key)
        data[key] = "" if value is None else str(value)
    data.update(overrides)
    return data


def get_title(db, slug: str) -> Title | None:
    db.expire_all()
    return db.scalar(select(Title).where(Title.slug == slug))


def create_payload(name: str, **extra) -> dict:
    data = {
        "name": name,
        "slug": "",
        "type": "series",
        "year": "2026",
        "added_at": "2026-11-08",
    }
    data.update(extra)
    return data


# ───────── configuration ─────────
@pytest.mark.parametrize("password", ["", "   "])
def test_blank_password_keeps_admin_off(db, password):
    api = FastAPI()
    assert (
        setup_admin(
            api, db.get_bind(), Settings(_env_file=None, admin_password=password)
        )
        is None
    )
    assert TestClient(api).get("/admin/login").status_code == 404


def test_short_password_refuses_to_start(db):
    with pytest.raises(ValueError, match="at least"):
        setup_admin(
            FastAPI(), db.get_bind(), Settings(_env_file=None, admin_password="short")
        )


def test_production_needs_a_secret_key():
    with pytest.raises(ValueError, match="ADMIN_SECRET_KEY"):
        AdminAuth(
            Settings(_env_file=None, admin_password=PASSWORD, admin_cookie_secure=True)
        )


# ───────── authentication ─────────
def test_every_admin_action_needs_login(admin_client, db):  # noqa: F811
    t = get_title(db, "hollow-tide")
    for method, url, data in [
        ("POST", "/admin/title/create", {"name": "x"}),
        ("POST", f"/admin/title/edit/{t.id}", {"name": "x"}),
        ("DELETE", f"/admin/title/delete?pks={t.id}", None),
        ("GET", "/admin/title/export/csv", None),
        ("GET", "/admin/episode/ajax/lookup?name=video&term=a", None),
        ("GET", f"/admin/title/details/{t.id}", None),
    ]:
        r = admin_client.request(method, url, data=data, follow_redirects=False)
        assert r.status_code in (302, 303), (method, url, r.status_code)
        assert "/admin/login" in r.headers["location"]
    assert get_title(db, "hollow-tide").name == "Hollow Tide"


def test_wrong_password_status(admin_client):  # noqa: F811
    assert login(admin_client, "wrong-password-xyz").status_code == 400


def test_lockout_after_repeated_failures(admin_client):  # noqa: F811
    for _ in range(MAX_FAILURES):
        login(admin_client, "wrong-password-xyz")
    # even the right password is refused while locked out
    assert login(admin_client).status_code == 400
    assert admin_client.get(
        "/admin/title/list", follow_redirects=False
    ).status_code in (302, 303)


def test_changing_password_ends_existing_sessions(db):
    key = "k" * 32
    api_old, api_new = FastAPI(), FastAPI()
    setup_admin(
        api_old,
        db.get_bind(),
        Settings(_env_file=None, admin_password=PASSWORD, admin_secret_key=key),
    )
    setup_admin(
        api_new,
        db.get_bind(),
        Settings(
            _env_file=None, admin_password=PASSWORD + "-new", admin_secret_key=key
        ),
    )
    old = TestClient(api_old)
    assert login(old).status_code in (302, 303)
    new = TestClient(api_new, cookies=old.cookies)
    assert new.get("/admin/title/list", follow_redirects=False).status_code in (
        302,
        303,
    )


def test_session_cookie_attributes(admin_client):  # noqa: F811
    cookie = login(admin_client).headers["set-cookie"].lower()
    assert "path=/admin" in cookie
    assert "samesite=strict" in cookie
    assert "httponly" in cookie
    assert "max-age=28800" in cookie


def test_cross_origin_posts_are_blocked(admin_client, db):  # noqa: F811
    login(admin_client)
    t = get_title(db, "hollow-tide")
    r = admin_client.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, name="Hacked"),
        headers={"Origin": "http://localhost:3000"},
    )
    assert r.status_code == 403
    assert get_title(db, "hollow-tide").name == "Hollow Tide"
    r = admin_client.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)


def test_export_is_disabled(admin_client):  # noqa: F811
    login(admin_client)
    assert admin_client.get("/admin/title/export/csv").status_code in (403, 404)


# ───────── saving titles ─────────
def test_genre_failure_saves_nothing(admin_client, db, monkeypatch):  # noqa: F811
    async def boom(self, names):
        raise RuntimeError("database hiccup")

    monkeypatch.setattr(TitleAdmin, "_genre_ids", boom)
    login(admin_client)
    r = admin_client.post(
        "/admin/title/create",
        data=create_payload("Half Saved", genres_csv="Drama"),
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert get_title(db, "half-saved") is None


def test_too_long_genre_is_rejected(admin_client, db):  # noqa: F811
    login(admin_client)
    r = admin_client.post(
        "/admin/title/create",
        data=create_payload("Long Genre", genres_csv="x" * 61),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "at most 60" in r.text
    assert get_title(db, "long-genre") is None


def test_edit_keeps_everything_not_changed(admin_client, db):  # noqa: F811
    login(admin_client)

    def snapshot(t: Title):
        return (
            t.genres,
            t.video_id,
            t.sub,
            t.dub,
            t.poster_url,
            t.poster_focus_x,
            t.cast_members,
        )

    for slug in ("the-last-lighthouse", "echoes-of-the-starfall"):
        t = get_title(db, slug)
        before, seasons = snapshot(t), len(t.seasons)
        r = admin_client.post(
            f"/admin/title/edit/{t.id}", data=title_form(t), follow_redirects=False
        )
        assert r.status_code in (302, 303), r.text[:300]
        t = get_title(db, slug)
        assert snapshot(t) == before and len(t.seasons) == seasons


def test_slug_rules(admin_client, db):  # noqa: F811
    login(admin_client)
    # nothing usable in the name: asks for a slug
    r = admin_client.post(
        "/admin/title/create", data=create_payload("!!!"), follow_redirects=False
    )
    assert r.status_code == 400 and "slug" in r.text.lower()
    # typed slugs are cleaned up
    r = admin_client.post(
        "/admin/title/create",
        data=create_payload("Shingeki", slug="My Show / 2"),
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    assert get_title(db, "my-show-2") is not None
    # duplicates get a friendly message, not a raw database error
    r = admin_client.post(
        "/admin/title/create",
        data=create_payload("Hollow Tide"),
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "already used" in r.text and "IntegrityError" not in r.text


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"poster_focus_x": "500"}, "between 0 and 100"),
        ({"duration_sec": "-5"}, "negative"),
        ({"intro_start_sec": "300", "intro_end_sec": "10"}, "after intro start"),
        ({"intro_start_sec": "10", "intro_end_sec": "90", "outro_start_sec": "60"}, "after the intro ends"),
        ({"duration_sec": "6000", "outro_start_sec": "6000"}, "before the end of the video"),
        ({"colors_csv": "red; x"}, "two hex colours"),
    ],
)
def test_validation(admin_client, db, overrides, message):  # noqa: F811
    login(admin_client)
    t = get_title(db, "the-last-lighthouse")
    r = admin_client.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, **overrides),
        follow_redirects=False,
    )
    assert r.status_code == 400, r.status_code
    assert message in r.text


def test_outro_is_saved_and_zero_means_none(admin_client, db):  # noqa: F811
    login(admin_client)
    t = get_title(db, "the-last-lighthouse")
    r = admin_client.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, duration_sec="6000", outro_start_sec="5700"),
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    assert get_title(db, "the-last-lighthouse").outro_start_sec == 5700
    t = get_title(db, "the-last-lighthouse")
    admin_client.post(f"/admin/title/edit/{t.id}", data=title_form(t, outro_start_sec="0"))
    assert get_title(db, "the-last-lighthouse").outro_start_sec is None


def test_empty_colours_use_default(admin_client, db):  # noqa: F811
    login(admin_client)
    t = get_title(db, "hollow-tide")
    admin_client.post(f"/admin/title/edit/{t.id}", data=title_form(t, colors_csv=""))
    assert get_title(db, "hollow-tide").colors == ["#3a3a4a", "#22222b"]


# ───────── deleting ─────────
def test_deleting_title_deletes_its_videos(admin_client, db):  # noqa: F811
    login(admin_client)
    t = get_title(db, "hollow-tide")
    video_ids = [e.video_id for s in t.seasons for e in s.episodes if e.video_id]
    assert video_ids
    admin_client.delete(f"/admin/title/delete?pks={t.id}")
    assert get_title(db, "hollow-tide") is None
    assert db.scalar(select(Video).where(Video.id.in_(video_ids))) is None


def test_deleting_episode_deletes_its_video(admin_client, db):  # noqa: F811
    login(admin_client)
    ep = db.get(Episode, "hollow-tide-s1e1")
    video_id = ep.video_id
    admin_client.delete(f"/admin/episode/delete?pks={ep.id}")
    db.expire_all()
    assert db.get(Episode, "hollow-tide-s1e1") is None
    assert db.get(Video, video_id) is None


def test_video_in_use_cannot_be_deleted(admin_client, db):  # noqa: F811
    login(admin_client)
    video_id = db.get(Episode, "hollow-tide-s1e2").video_id
    r = admin_client.delete(f"/admin/video/delete?pks={video_id}")
    assert "used+by+an+episode" in r.text or "used by an episode" in r.text
    db.expire_all()
    assert db.get(Video, video_id) is not None


def test_video_search_only_offers_unused_videos(admin_client, db):  # noqa: F811
    login(admin_client)
    spare = Video(provider=VideoProvider.external, url="https://example.com/spare.m3u8")
    db.add(spare)
    db.commit()
    r = admin_client.get(
        "/admin/episode/ajax/lookup", params={"name": "video", "term": "m3u8"}
    )
    ids = {item["id"] for item in r.json()["results"]}
    assert spare.id in ids
    assert db.get(Episode, "hollow-tide-s1e1").video_id not in ids


@pytest.mark.parametrize(
    ("data", "outro", "error"),
    [
        ({"outro_start_sec": 1400, "intro_end_sec": 90, "duration_sec": 1500}, 1400, None),
        ({"outro_start_sec": 0, "duration_sec": 1500}, None, None),  # 0 = no credits
        ({"outro_start_sec": None}, None, None),
        ({"outro_start_sec": 1400, "duration_sec": 0}, 1400, None),  # length unknown: no upper bound
        ({"outro_start_sec": 60, "intro_end_sec": 90}, None, "after the intro ends"),
        ({"outro_start_sec": 1500, "duration_sec": 1500}, None, "before the end of the video"),
    ],
)
def test_check_outro(data, outro, error):
    from app.admin.views import check_outro

    if error:
        with pytest.raises(ValueError, match=error):
            check_outro(dict(data))
    else:
        data = dict(data)
        check_outro(data)
        assert data["outro_start_sec"] == outro
