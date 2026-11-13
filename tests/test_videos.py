"""Videos are links (Cloudflare R2): the admin form, and the migration away from Mux."""

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text

from app.admin.views import check_video_url
from app.config import PROJECT_ROOT, get_settings
from app.db import make_engine
from app.models import Episode, Video, VideoProvider
from app.services.video_url import subtitle_tracks
from tests.test_admin import admin_client, login  # noqa: F401  (fixture)

R2_LINK = "https://pub-abc123.r2.dev/videos/episode-1-3f9a2c/master.m3u8"


# ───────── the link check ─────────
@pytest.mark.parametrize(
    "url",
    [
        R2_LINK,
        "https://media.reelm.tv/videos/ep-1-aaaaaa/master.m3u8",
        "https://example.com/clip.MP4",
        "http://localhost:9000/videos/x/master.m3u8?v=2",
    ],
)
def test_good_links_are_accepted(url):
    assert check_video_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "",
        None,
        "pub-abc123.r2.dev/videos/episode-1-3f9a2c/master.m3u8",  # no https://
        "https://pub-abc123.r2.dev/videos/episode-1-3f9a2c/",  # the folder, not the file
        "https://pub-abc123.r2.dev/videos/episode-1-3f9a2c/v1080p/seg_00000.ts",
        "ftp://example.com/x.m3u8",
        "https://example.com/my video.m3u8",
    ],
)
def test_bad_links_are_refused(url):
    with pytest.raises(ValueError, match="master.m3u8"):
        check_video_url(url)


# ───────── the admin form ─────────
def test_add_a_video_by_link(admin_client, db):  # noqa: F811
    login(admin_client)
    assert admin_client.get("/admin/video/create").status_code == 200
    r = admin_client.post(
        "/admin/video/create",
        data={"url": f" {R2_LINK} ", "duration_sec": "1542"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:500]
    video = db.scalar(select(Video).where(Video.url == R2_LINK))
    assert video.provider == VideoProvider.external and video.duration_sec == 1542


def test_a_wrong_link_is_explained(admin_client, db):  # noqa: F811
    login(admin_client)
    folder_only = "https://pub-abc123.r2.dev/videos/episode-1-3f9a2c/"
    r = admin_client.post(
        "/admin/video/create", data={"url": folder_only}, follow_redirects=False
    )
    assert r.status_code == 400 and "master.m3u8" in r.text
    assert db.scalar(select(Video).where(Video.url == folder_only)) is None


def test_video_list_page_works(admin_client):  # noqa: F811
    login(admin_client)
    assert admin_client.get("/admin/video/list").status_code == 200


def test_mux_endpoints_are_gone(client):
    assert client.post("/webhooks/mux").status_code == 404


# ───────── subtitles ─────────
def test_subtitle_files_sit_next_to_the_master_playlist():
    v = Video(url=R2_LINK, subtitles=["en", "es"])
    base = R2_LINK.rsplit("/", 1)[0]
    assert subtitle_tracks(v) == [
        {"lang": "en", "url": f"{base}/en.vtt"},
        {"lang": "es", "url": f"{base}/es.vtt"},
    ]
    assert subtitle_tracks(Video(url=R2_LINK)) == []
    assert subtitle_tracks(Video(subtitles=["en"])) == [], "no link, nothing to play"
    assert subtitle_tracks(None) == []


def test_player_gets_the_subtitles(client, db):
    ep = db.get(Episode, "echoes-of-the-starfall-s1e3")
    ep.video = Video(url=R2_LINK, subtitles=["en"])
    db.commit()
    p = client.get("/playback/echoes-of-the-starfall", params={"ep": ep.id}).json()
    assert p["subtitles"] == [
        {"lang": "en", "url": R2_LINK.rsplit("/", 1)[0] + "/en.vtt"}
    ]
    other = client.get(
        "/playback/echoes-of-the-starfall", params={"ep": "echoes-of-the-starfall-s1e1"}
    ).json()
    assert other["subtitles"] == []


def test_admin_subtitle_languages(admin_client, db):  # noqa: F811
    login(admin_client)
    r = admin_client.post(
        "/admin/video/create",
        data={"url": R2_LINK, "duration_sec": "1542", "subtitles_csv": " en, ES ,en "},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:500]
    video = db.scalar(select(Video).where(Video.url == R2_LINK))
    assert video.subtitles == ["en", "es"]
    edit = admin_client.get(f"/admin/video/edit/{video.id}")
    assert edit.status_code == 200 and 'value="en, es"' in edit.text

    bad = admin_client.post(
        "/admin/video/create",
        data={
            "url": R2_LINK.replace("episode-1", "episode-2"),
            "subtitles_csv": "English",
        },
        follow_redirects=False,
    )
    assert bad.status_code == 400 and "en.vtt" in bad.text


# ───────── a video left over from Mux (before the migration has run) ─────────
def test_leftover_mux_video_breaks_nothing(client, admin_client, db):  # noqa: F811
    """Until it's replaced, the old Mux video just doesn't play; no page may crash,
    least of all the admin pages used to replace it."""
    ep = db.get(Episode, "echoes-of-the-starfall-s1e3")
    old = Video(provider=VideoProvider.mux)
    db.add(old)
    ep.video = old
    db.commit()

    title = client.get("/titles/echoes-of-the-starfall")
    assert title.status_code == 200
    listed = title.json()["seasons"][0]["episodes"][2]
    assert "videoUrl" not in listed
    assert (
        client.get("/playback/echoes-of-the-starfall", params={"ep": ep.id}).status_code
        == 404
    )

    login(admin_client)
    for page in (
        "/admin/episode/list",
        f"/admin/episode/edit/{ep.id}",
        "/admin/video/list",
        f"/admin/video/details/{old.id}",
    ):
        r = admin_client.get(page)
        assert r.status_code == 200, page
    assert "Old Mux video" in admin_client.get("/admin/video/list").text

    # replace it: a new R2 video for the episode, then the old one can go
    new = Video(url=R2_LINK, duration_sec=1542)
    db.add(new)
    ep.video = new
    db.commit()
    old_id = old.id
    r = admin_client.delete(f"/admin/video/delete?pks={old_id}")
    assert r.status_code == 200
    db.expire_all()
    assert db.get(Video, old_id) is None
    played = client.get("/playback/echoes-of-the-starfall", params={"ep": ep.id})
    assert played.json()["videoUrl"] == R2_LINK


# ───────── the migration away from Mux ─────────
def test_migration_refuses_while_a_mux_video_exists(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'mux.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    engine = make_engine(url)
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    insert = text(
        "INSERT INTO videos (id, provider, status, playback_id) "
        "VALUES ('m1', 'mux', 'ready', 'abc')"
    )
    try:
        command.upgrade(cfg, "a7c1d9e2b4f0")  # the last version that had Mux
        with engine.begin() as conn:
            conn.execute(insert)

        with pytest.raises(RuntimeError, match="still use Mux"):
            command.upgrade(cfg, "head")
        with engine.connect() as conn:  # nothing changed
            assert conn.scalar(text("SELECT playback_id FROM videos")) == "abc"

        with engine.begin() as conn:  # after replacing it in the admin...
            conn.execute(text("DELETE FROM videos WHERE provider = 'mux'"))
        command.upgrade(cfg, "head")  # ...it goes through
        with engine.connect() as conn:
            columns = {r[1] for r in conn.execute(text("PRAGMA table_info(videos)"))}
        assert columns == {
            "id",
            "provider",
            "url",
            "duration_sec",
            "subtitles",
            "created_at",
            "updated_at",
        }
    finally:
        engine.dispose()
        get_settings.cache_clear()
