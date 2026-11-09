from sqlalchemy import func, select

from app.models import Episode, Title, Video, VideoProvider, VideoStatus
from app.seed import seed
from app.services.video_url import playback_url


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def test_seed_loads_full_catalogue(db, catalogue):
    assert count(db, Title) == len(catalogue) == 31
    assert count(db, Episode) == sum(
        len(s["episodes"]) for t in catalogue for s in t.get("seasons") or []
    )


def test_seed_is_idempotent(db, catalogue):
    counts = seed(db, catalogue)
    assert counts == {"titles": 0, "episodes": 0, "skipped": 31}
    assert count(db, Title) == 31


def test_seed_reset_reloads(db, catalogue):
    counts = seed(db, catalogue, reset=True)
    assert counts["titles"] == 31
    assert count(db, Title) == 31


def test_genre_order_is_preserved(db):
    t = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    assert t.genres == ["Sci-Fi", "Action", "Drama"]


def test_videos_are_deduplicated_by_url(db, catalogue):
    urls = {t.get("videoUrl") for t in catalogue} | {
        e["videoUrl"]
        for t in catalogue
        for s in t.get("seasons") or []
        for e in s["episodes"]
    }
    urls.discard(None)
    assert count(db, Video) == len(urls)


def test_playback_url_per_provider():
    assert playback_url(None) is None
    mux = Video(
        provider=VideoProvider.mux, playback_id="abc123", status=VideoStatus.ready
    )
    assert playback_url(mux) == "https://stream.mux.com/abc123.m3u8"
    ext = Video(
        provider=VideoProvider.external,
        url="https://x/y.m3u8",
        status=VideoStatus.ready,
    )
    assert playback_url(ext) == "https://x/y.m3u8"
    not_ready = Video(
        provider=VideoProvider.mux, playback_id="abc123", status=VideoStatus.waiting
    )
    assert playback_url(not_ready) is None


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
