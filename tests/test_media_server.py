"""The read-only media server (app/media_server.py)."""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media_server import create_app

JPEG = b"\xff\xd8\xffjpeg"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows file system")


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, TestClient]:
    (tmp_path / "images" / "posters").mkdir(parents=True)
    (tmp_path / "images" / "posters" / "a.jpg").write_bytes(JPEG)
    (tmp_path / "images" / "stills").mkdir()
    (tmp_path / "images" / "stills" / "cave.jpg").write_bytes(JPEG)
    (tmp_path / "images" / "posters" / "desktop.ini").write_text("[x]")
    # real layout: videos/<show>/<episode folder>/...
    ep = tmp_path / "videos" / "my-show" / "ep1-abc123"
    (ep / "v720p").mkdir(parents=True)
    (ep / "master.m3u8").write_text("#EXTM3U\n")
    (ep / "en.vtt").write_text("WEBVTT\n")
    (ep / "v720p" / "seg_00000.ts").write_bytes(b"G" * 1000)
    for busy in (
        tmp_path / "videos" / "my-show" / "ep2-busy",  # nested, still encoding
        tmp_path / "videos" / "flat-busy",  # directly under videos/
    ):
        busy.mkdir(parents=True)
        (busy / "master.m3u8").write_text("#EXTM3U\n")
        (busy / ".incomplete").touch()
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "p.jpg").write_bytes(JPEG)
    (tmp_path / "secret.txt").write_text("nope")
    app = create_app(Settings(_env_file=None, media_root=tmp_path))
    return tmp_path, TestClient(app)


def test_serves_images_and_hls_with_the_right_types(media):
    _, c = media
    r = c.get("/images/posters/a.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "public, max-age=604800, immutable"
    r = c.get("/videos/my-show/ep1-abc123/master.m3u8")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert r.headers["cache-control"] == "public, max-age=60"
    r = c.get("/videos/my-show/ep1-abc123/v720p/seg_00000.ts")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp2t"
    r = c.get("/videos/my-show/ep1-abc123/en.vtt")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/vtt")


def test_stills_keep_their_names_so_are_cached_briefly(media):
    _, c = media
    r = c.get("/images/stills/cave.jpg")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=3600"


def test_cors_lets_any_site_play(media):
    _, c = media
    r = c.get(
        "/videos/my-show/ep1-abc123/master.m3u8",
        headers={"Origin": "https://reelm.vercel.app"},
    )
    assert r.headers["access-control-allow-origin"] == "*"


def test_range_requests(media):
    _, c = media
    r = c.get(
        "/videos/my-show/ep1-abc123/v720p/seg_00000.ts",
        headers={"Range": "bytes=0-99"},
    )
    assert r.status_code == 206 and len(r.content) == 100


@pytest.mark.parametrize(
    "path",
    [
        "/secret.txt",
        "/private/p.jpg",
        "/",
        "/images/",
        "/images/posters/",
        "/videos/my-show/ep1-abc123/",
        "/images/posters/missing.jpg",
        # folders still being encoded, at any depth
        "/videos/my-show/ep2-busy/master.m3u8",
        "/videos/flat-busy/master.m3u8",
        "/videos/flat-busy%20/master.m3u8",
        "/videos/flat-busy./master.m3u8",
        "/videos/my-show/ep2-busy/.incomplete",
        # traversal
        "/images/../secret.txt",
        "/images/%2e%2e/secret.txt",
        "/images/..%5csecret.txt",
        "/images/C:/x.jpg",
        # Windows aliases of a real file
        "/IMAGES/posters/a.jpg",
        "/images/posters/A.JPG",
        "/images/posters/a.jpg.",
        "/images/posters/a.jpg%20",
        "/images/posters/a.jpg::$DATA",
        "/images/posters/a.jpg:Zone.Identifier",
        # not media
        "/images/posters/desktop.ini",
        "/images/posters/a.m3u8",
    ],
)
def test_nothing_else_is_reachable(media, path):
    _, c = media
    r = c.get(path)
    assert r.status_code == 404, path
    assert r.headers.get("cache-control") == "no-store"


@windows_only
def test_ntfs_streams_are_not_served(media):
    root, c = media
    with open(root / "images" / "posters" / "a.jpg:Zone.Identifier", "w") as f:
        f.write("[ZoneTransfer]\nHostUrl=https://example.com/a.jpg\n")
    r = c.get("/images/posters/a.jpg:Zone.Identifier")
    assert r.status_code == 404 and b"HostUrl" not in r.content
    assert c.get("/images/posters/a.jpg").content == JPEG


@windows_only
def test_junctions_cannot_expose_other_folders(media):
    import _winapi

    root, c = media
    _winapi.CreateJunction(str(root / "private"), str(root / "images" / "j"))
    assert (root / "images" / "j" / "p.jpg").exists()
    assert c.get("/images/j/p.jpg").status_code == 404


def test_read_only(media):
    root, c = media
    for method in ("PUT", "POST", "DELETE"):
        r = c.request(method, "/images/posters/a.jpg", content=b"x")
        assert r.status_code in (404, 405)
    assert (root / "images" / "posters" / "a.jpg").read_bytes() == JPEG
