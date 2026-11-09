from sqlalchemy import func, select

from app.models import (
    Episode,
    Genre,
    Season,
    Title,
    TitleGenre,
    Video,
    VideoProvider,
    VideoStatus,
)
from app.seed import seed
from app.services.video_url import playback_url

ALL_MODELS = (Title, Season, Episode, Genre, TitleGenre, Video)


def count(db, model) -> int:
    return db.scalar(select(func.count()).select_from(model))


def counts(db) -> dict[str, int]:
    return {m.__name__: count(db, m) for m in ALL_MODELS}


def expected_videos(catalogue) -> int:
    movies = sum(1 for t in catalogue if t.get("videoUrl"))
    episodes = sum(
        1
        for t in catalogue
        for s in t.get("seasons") or []
        for e in s["episodes"]
        if e.get("videoUrl")
    )
    return movies + episodes


def test_seed_loads_full_catalogue(db, catalogue):
    assert count(db, Title) == len(catalogue) == 31
    assert count(db, Episode) == sum(
        len(s["episodes"]) for t in catalogue for s in t.get("seasons") or []
    )


def test_every_episode_and_movie_has_its_own_video(db, catalogue):
    assert count(db, Video) == expected_videos(catalogue)
    shared = db.execute(
        select(Episode.video_id).group_by(Episode.video_id).having(func.count() > 1)
    ).all()
    assert shared == []


def test_seed_is_idempotent(db, catalogue):
    before = counts(db)
    assert seed(db, catalogue) == {"titles": 0, "episodes": 0, "skipped": 31}
    assert counts(db) == before


def test_seed_skips_when_id_exists_under_another_slug(db, catalogue):
    renamed = dict(catalogue[0], slug="brand-new-slug")
    assert seed(db, [renamed])["skipped"] == 1


def test_seed_reset_reloads_everything(db, catalogue):
    before = counts(db)
    # touch loaded objects first: reset must not reuse stale session state
    loaded = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    _ = [e.name for s in loaded.seasons for e in s.episodes]

    assert seed(db, catalogue, reset=True)["titles"] == 31
    assert counts(db) == before
    reloaded = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    assert reloaded is not loaded
    assert reloaded.genres == ["Sci-Fi", "Action", "Drama"]


def test_genre_order_is_preserved(db):
    t = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    assert t.genres == ["Sci-Fi", "Action", "Drama"]


def test_deleting_a_genre_removes_its_links(db):
    drama = db.scalar(select(Genre).where(Genre.name == "Drama"))
    linked = db.scalar(
        select(func.count())
        .select_from(TitleGenre)
        .where(TitleGenre.genre_id == drama.id)
    )
    assert linked > 0
    db.delete(drama)
    db.commit()
    db.expire_all()
    orphans = db.scalar(
        select(func.count())
        .select_from(TitleGenre)
        .where(TitleGenre.genre_id == drama.id)
    )
    assert orphans == 0
    t = db.scalar(select(Title).where(Title.slug == "echoes-of-the-starfall"))
    assert t.genres == ["Sci-Fi", "Action"]


def test_deleting_a_video_unlinks_the_episode(db):
    ep = db.get(Episode, "echoes-of-the-starfall-s1e1")
    video_id = ep.video_id
    assert video_id
    db.delete(db.get(Video, video_id))
    db.commit()
    db.expire_all()
    assert db.get(Episode, "echoes-of-the-starfall-s1e1").video_id is None


def test_new_rows_get_generated_ids(db):
    v = Video(provider=VideoProvider.mux)
    db.add(v)
    db.flush()
    assert v.id and v.status == VideoStatus.waiting


def test_playback_url_per_provider():
    assert playback_url(None) is None

    mux = Video(
        provider=VideoProvider.mux, playback_id="abc123", status=VideoStatus.ready
    )
    assert playback_url(mux) == "https://stream.mux.com/abc123.m3u8"
    mux_waiting = Video(
        provider=VideoProvider.mux, playback_id="abc", status=VideoStatus.waiting
    )
    assert playback_url(mux_waiting) is None
    mux_no_id = Video(provider=VideoProvider.mux, status=VideoStatus.ready)
    assert playback_url(mux_no_id) is None

    ext = Video(
        provider=VideoProvider.external,
        url="https://x/y.m3u8",
        status=VideoStatus.ready,
    )
    assert playback_url(ext) == "https://x/y.m3u8"
    # external URLs need no processing, so they play even if never marked ready
    ext_new = Video(provider=VideoProvider.external, url="https://x/z.mp4")
    assert playback_url(ext_new) == "https://x/z.mp4"
    assert playback_url(Video(provider=VideoProvider.external)) is None


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
