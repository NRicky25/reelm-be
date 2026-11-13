"""The read-only media server (app/media_server.py)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.media_server import create_app


@pytest.fixture
def media(tmp_path: Path) -> tuple[Path, TestClient]:
    (tmp_path / "images" / "posters").mkdir(parents=True)
    (tmp_path / "images" / "posters" / "a.jpg").write_bytes(b"\xff\xd8\xffjpeg")
    show = tmp_path / "videos" / "show-ep1-abc123"
    (show / "v720p").mkdir(parents=True)
    (show / "master.m3u8").write_text("#EXTM3U\n")
    (show / "v720p" / "seg_00000.ts").write_bytes(b"G" * 1000)
    busy = tmp_path / "videos" / "still-encoding"
    busy.mkdir()
    (busy / "master.m3u8").write_text("#EXTM3U\n")
    (busy / ".incomplete").touch()
    (tmp_path / "secret.txt").write_text("nope")
    app = create_app(Settings(_env_file=None, media_root=tmp_path))
    return tmp_path, TestClient(app)


def test_serves_images_and_hls_with_the_right_types(media):
    _, c = media
    r = c.get("/images/posters/a.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    r = c.get("/videos/show-ep1-abc123/master.m3u8")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert r.headers["cache-control"] == "public, max-age=60"
    r = c.get("/videos/show-ep1-abc123/v720p/seg_00000.ts")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp2t"
    assert "max-age=604800" in r.headers["cache-control"]


def test_cors_lets_any_site_play(media):
    _, c = media
    r = c.get(
        "/videos/show-ep1-abc123/master.m3u8",
        headers={"Origin": "https://reelm.vercel.app"},
    )
    assert r.headers["access-control-allow-origin"] == "*"


def test_range_requests(media):
    _, c = media
    r = c.get(
        "/videos/show-ep1-abc123/v720p/seg_00000.ts", headers={"Range": "bytes=0-99"}
    )
    assert r.status_code == 206 and len(r.content) == 100


@pytest.mark.parametrize(
    "path",
    [
        "/secret.txt",
        "/",
        "/images/",
        "/videos/show-ep1-abc123/",
        "/videos/still-encoding/master.m3u8",
        "/videos/still-encoding/.incomplete",
        "/images/../secret.txt",
        "/images/%2e%2e/secret.txt",
        "/images/..%5csecret.txt",
    ],
)
def test_nothing_else_is_reachable(media, path):
    _, c = media
    assert c.get(path).status_code == 404


def test_read_only(media):
    root, c = media
    for method in ("PUT", "POST", "DELETE"):
        r = c.request(method, "/images/posters/a.jpg", content=b"x")
        assert r.status_code in (404, 405)
    assert (root / "images" / "posters" / "a.jpg").read_bytes() == b"\xff\xd8\xffjpeg"
