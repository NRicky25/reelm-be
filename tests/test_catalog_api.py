"""Catalogue API: shapes must match reelm-fe/src/data/types.ts."""

from datetime import date

import pytest

from app.config import get_settings
from app.models import Episode

TITLE_KEYS = {
    "id", "slug", "type", "name", "description", "year", "genres", "ageRating",
    "audioLanguages", "subtitleLanguages", "colors", "featured", "staffPick",
    "addedAt", "studio", "cast",
}  # fmt: skip


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    """Pin 'today' to the demo catalogue's date so NEW / released logic is stable."""
    get_settings.cache_clear()
    monkeypatch.setenv("CATALOG_TODAY", "2026-11-08")
    yield
    get_settings.cache_clear()


def test_title_shape_matches_frontend(client):
    t = client.get("/titles/echoes-of-the-starfall").json()
    assert TITLE_KEYS <= t.keys()
    assert t["genres"] == ["Sci-Fi", "Action", "Drama"]
    assert "durationSec" not in t and "videoUrl" not in t  # series: no movie fields
    ep = t["seasons"][1]["episodes"][6]
    assert ep["id"] == "echoes-of-the-starfall-s2e7"
    assert ep["releaseDate"] == "2026-10-29"
    assert ep["videoUrl"].endswith(".m3u8")
    assert {"durationSec", "thumbUrl", "introStartSec", "introEndSec"} <= ep.keys()


def test_movie_has_video_and_no_seasons(client):
    m = client.get("/titles/the-last-lighthouse").json()
    assert m["type"] == "movie"
    assert "seasons" not in m
    assert m["videoUrl"] and m["durationSec"] == 112 * 60
    assert m["cast"] == ["Elena Marsh", "Tomas Reyes", "Iris Hale"]


def test_unknown_title_is_404(client):
    assert client.get("/titles/nope").status_code == 404
    assert client.get("/titles/nope/related").status_code == 404


def test_filters(client):
    anime = client.get("/titles", params={"type": "anime"}).json()
    assert anime and all(t["type"] == "anime" for t in anime)

    featured = client.get("/titles", params={"featured": "true"}).json()
    assert {t["slug"] for t in featured} == {
        "echoes-of-the-starfall", "the-last-lighthouse", "mochi-detective", "glass-harbor",
    }  # fmt: skip

    picks = client.get("/titles", params={"staffPick": "true"}).json()
    assert picks and all(t["staffPick"] for t in picks)

    by_slug = client.get(
        "/titles", params={"genre": "slice-of-life", "type": "anime"}
    ).json()
    by_name = client.get(
        "/titles", params={"genre": "Slice of Life", "type": "anime"}
    ).json()
    assert by_slug == by_name and all("Slice of Life" in t["genres"] for t in by_slug)

    some = client.get("/titles", params={"slugs": "hollow-tide,flatmates"}).json()
    assert {t["slug"] for t in some} == {"hollow-tide", "flatmates"}

    assert len(client.get("/titles", params={"limit": 3}).json()) == 3


def test_lists(client):
    recent = client.get("/titles", params={"list": "recent"}).json()
    added = [t["addedAt"] for t in recent]
    assert added == sorted(added, reverse=True)

    az = [t["name"] for t in client.get("/titles", params={"list": "az"}).json()]
    assert az == sorted(az, key=str.lower)

    dub = client.get("/titles", params={"type": "anime", "list": "dub"}).json()
    assert dub and all(t.get("dub") for t in dub)

    assert client.get("/titles", params={"list": "nonsense"}).status_code == 422


def test_new_episodes_are_recent_and_sorted(client):
    new = client.get("/titles", params={"type": "anime", "list": "new"}).json()
    assert new
    on = date(2026, 11, 8)
    latest = [date.fromisoformat(t["latestEpisode"]["releaseDate"]) for t in new]
    assert all((on - d).days <= 14 for d in latest)
    assert latest == sorted(latest, reverse=True)


def test_latest_episode_in_list_matches_detail(client):
    summary = client.get("/titles", params={"slugs": "echoes-of-the-starfall"}).json()[
        0
    ]
    detail = client.get("/titles/echoes-of-the-starfall").json()
    on = date(2026, 11, 8)
    playable = [
        (e["releaseDate"], s["number"], e["number"], e["id"])
        for s in detail["seasons"]
        for e in s["episodes"]
        if e.get("videoUrl") and date.fromisoformat(e["releaseDate"]) <= on
    ]
    newest = max(playable)
    assert summary["latestEpisode"] == {
        "season": newest[1], "number": newest[2], "id": newest[3], "releaseDate": newest[0],
    }  # fmt: skip
    assert detail["latestEpisode"] == summary["latestEpisode"]


def test_lists_are_summaries_without_episodes(client):
    for path, params in [
        ("/titles", {}),
        ("/search", {"q": "star"}),
        ("/titles/echoes-of-the-starfall/related", {}),
    ]:
        items = client.get(path, params=params).json()
        assert items and all("seasons" not in t for t in items), path


def test_full_titles_by_slug(client):
    full = client.get(
        "/titles",
        params={
            "slugs": "echoes-of-the-starfall,the-last-lighthouse,nope",
            "full": "true",
        },
    ).json()
    by_slug = {t["slug"]: t for t in full}
    assert set(by_slug) == {
        "echoes-of-the-starfall",
        "the-last-lighthouse",
    }  # unknown slug ignored
    assert len(by_slug["echoes-of-the-starfall"]["seasons"]) == 2
    assert "seasons" not in by_slug["the-last-lighthouse"]
    assert client.get("/titles", params={"full": "true"}).status_code == 422


def test_full_titles_query_count_does_not_grow_with_slugs(client, db):
    """Regression: full=true used to run a few queries per slug (slow over the network)."""
    from sqlalchemy import event

    from app.models import Title

    slugs = [t.slug for t in db.query(Title).order_by(Title.slug)]
    statements: list[str] = []

    def count(_conn, _cursor, statement, *_args):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", count)
    try:
        counts = []
        for n in (2, len(slugs)):
            statements.clear()
            r = client.get(
                "/titles", params={"slugs": ",".join(slugs[:n]), "full": "true"}
            )
            assert len(r.json()) == n
            counts.append(len(statements))
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert counts[0] == counts[1], counts


def test_new_movies_and_mixed_types(client):
    movies = client.get("/titles", params={"type": "movie", "list": "new"}).json()
    added = [date.fromisoformat(t["addedAt"]) for t in movies]
    assert movies and all((date(2026, 11, 8) - d).days <= 84 for d in added)
    assert added == sorted(added, reverse=True)
    mixed = client.get("/titles", params={"list": "new"}).json()
    assert {t["type"] for t in mixed} >= {"anime", "movie"}  # movies aren't dropped


def test_limit_applies_after_list(client):
    top3 = client.get(
        "/titles", params={"type": "anime", "list": "new", "limit": 3}
    ).json()
    everything = client.get("/titles", params={"type": "anime", "list": "new"}).json()
    assert [t["slug"] for t in top3] == [t["slug"] for t in everything][:3]


def test_search_treats_wildcards_literally(client):
    assert client.get("/search", params={"q": "%"}).json() == []
    assert client.get("/search", params={"q": "_"}).json() == []
    assert [
        t["slug"] for t in client.get("/search", params={"q": "Cinder & Salt"}).json()
    ] == ["cinder-and-salt"]


def test_related_shares_a_genre(client):
    rel = client.get("/titles/echoes-of-the-starfall/related").json()
    assert rel and all(t["slug"] != "echoes-of-the-starfall" for t in rel)
    assert all({"Sci-Fi", "Action", "Drama"} & set(t["genres"]) for t in rel)


def test_search(client):
    names = [t["name"] for t in client.get("/search", params={"q": "star"}).json()]
    assert names[0] == "Starlit Courier"  # name starts with the query: ranked first
    assert set(names) == {
        "Starlit Courier",
        "Echoes of the Starfall",
        "Lone Star Diner",
    }
    # genre match, no duplicates even though titles have several genres
    docs = client.get("/search", params={"q": "documentary"}).json()
    assert len(docs) == len({t["id"] for t in docs}) == 2
    assert client.get("/search", params={"q": "   "}).json() == []


def test_genres(client):
    anime = client.get("/genres", params={"type": "anime"}).json()
    assert "Slice of Life" in anime and anime == sorted(anime)
    assert "Reality" not in anime
    assert "Reality" in client.get("/genres", params={"type": "series"}).json()


def test_playback_episode(client):
    p = client.get(
        "/playback/echoes-of-the-starfall", params={"ep": "echoes-of-the-starfall-s2e7"}
    ).json()
    assert p["season"] == 2 and p["episode"]["number"] == 7
    assert p["videoUrl"].endswith(".m3u8")
    assert p["introStartSec"] == 5 and p["introEndSec"] == 35
    assert p["next"] == {"season": 2, "episodeId": "echoes-of-the-starfall-s2e8"}
    assert p["title"]["slug"] == "echoes-of-the-starfall"
    assert "outroStartSec" not in p  # not set: left out, like the other empty fields


def test_playback_outro(client, db):
    from sqlalchemy import select

    from app.models import Episode

    ep = db.scalar(select(Episode).where(Episode.id == "echoes-of-the-starfall-s2e7"))
    ep.outro_start_sec = 1300
    db.commit()
    p = client.get(
        "/playback/echoes-of-the-starfall", params={"ep": "echoes-of-the-starfall-s2e7"}
    ).json()
    assert p["outroStartSec"] == 1300
    assert p["episode"]["outroStartSec"] == 1300


def test_playback_defaults_to_first_episode_and_crosses_seasons(client):
    first = client.get("/playback/echoes-of-the-starfall").json()
    assert first["episode"]["id"] == "echoes-of-the-starfall-s1e1"
    end_of_s1 = client.get(
        "/playback/echoes-of-the-starfall",
        params={"ep": "echoes-of-the-starfall-s1e12"},
    ).json()
    assert end_of_s1["next"] == {
        "season": 2,
        "episodeId": "echoes-of-the-starfall-s2e1",
    }


def test_playback_by_season_and_episode_number(client):
    """Readable watch links (/watch/<slug>/s2/e7) look episodes up by number."""
    p = client.get("/playback/echoes-of-the-starfall", params={"s": 2, "e": 7}).json()
    assert p["episode"]["id"] == "echoes-of-the-starfall-s2e7" and p["season"] == 2
    missing = client.get("/playback/echoes-of-the-starfall", params={"s": 9, "e": 1})
    assert missing.status_code == 404
    future = client.get("/playback/echoes-of-the-starfall", params={"s": 2, "e": 12})
    assert future.status_code == 404
    only_one = client.get("/playback/echoes-of-the-starfall", params={"s": 2})
    assert only_one.status_code == 422


def test_playback_movie(client):
    p = client.get("/playback/the-last-lighthouse").json()
    assert "episode" not in p and "next" not in p
    assert p["videoUrl"].endswith(".mp4") and p["durationSec"] == 112 * 60


def test_playback_unavailable(client, db):
    # future episode
    assert (
        client.get(
            "/playback/echoes-of-the-starfall",
            params={"ep": "echoes-of-the-starfall-s2e12"},
        ).status_code
        == 404
    )
    # released episode whose video was removed
    ep = db.get(Episode, "echoes-of-the-starfall-s1e3")
    ep.video = None
    db.commit()
    r = client.get("/playback/echoes-of-the-starfall", params={"ep": ep.id})
    assert r.status_code == 404
    listed = client.get("/titles/echoes-of-the-starfall").json()["seasons"][0][
        "episodes"
    ][2]
    assert "videoUrl" not in listed
    assert client.get("/playback/nope").status_code == 404


def test_cors_allows_frontend(client):
    r = client.get("/titles", headers={"Origin": "http://localhost:3000"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:3000"
