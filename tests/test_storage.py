"""Image storage: the local media folder and admin image uploads."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.admin.auth as auth_module
from app.admin import setup_admin
from app.config import Settings
from app.models import Episode, Title
from app.services.storage import (
    MAX_BYTES,
    MediaStorage,
    StorageError,
    sniff_image_type,
)
from tests.test_admin_hardening import title_form

PASSWORD = "test-password-123"
BASE = "https://media.example.com"
PREFIX = f"{BASE}/images/"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
OTHER_SLUG = "neon-teahouse"
STILL = (
    Path(__file__).resolve().parents[2] / "reelm-fe" / "public" / "images" / "stills"
)


class FakeStorage:
    """A real MediaStorage in a temp folder that also records deletes / can fail."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.deleted: list[str] = []
        self.fail_folder: str | None = None  # uploads into this folder fail

    @property
    def objects(self) -> dict[str, bytes]:
        images = self.root / "images"
        return {
            f.relative_to(images).as_posix(): f.read_bytes()
            for f in images.rglob("*")
            if f.is_file()
        }

    def client(self) -> MediaStorage:
        fake = self

        class Recording(MediaStorage):
            def save(self, path: str, data: bytes) -> str:
                if fake.fail_folder and path.startswith(fake.fail_folder):
                    raise StorageError("boom")
                return super().save(path, data)

            def delete(self, paths: list[str]) -> None:
                fake.deleted.extend(paths)
                super().delete(paths)

        return Recording(self.root, BASE)


@pytest.fixture
def fake_storage(tmp_path) -> FakeStorage:
    return FakeStorage(tmp_path)


def test_sniffing_uses_content_not_filename():
    assert sniff_image_type(JPEG) == "image/jpeg"
    assert sniff_image_type(PNG) == "image/png"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_image_type(b"<svg onload=alert(1)>") is None
    assert sniff_image_type(b"GIF89a") is None


def test_upload_and_urls(fake_storage):
    st = fake_storage.client()
    url = st.upload("posters", JPEG)
    path = st.path_from_url(url)
    assert url.startswith(f"{PREFIX}posters/") and url.endswith(".jpg")
    assert fake_storage.objects[path] == JPEG
    assert (fake_storage.root / "images" / path).read_bytes() == JPEG
    assert st.path_from_url("https://elsewhere.example/x.jpg") is None
    assert st.upload("stills", PNG, name="fixed").endswith("/stills/fixed.png")
    assert st.upload("stills", JPEG, name="fixed").endswith("/stills/fixed.jpg")


def test_upload_rejects_bad_files(fake_storage):
    st = fake_storage.client()
    with pytest.raises(StorageError, match="Only JPEG"):
        st.upload("posters", b"<html>")
    with pytest.raises(StorageError, match="too large"):
        st.upload("posters", JPEG + b"0" * MAX_BYTES)
    assert fake_storage.objects == {}


@pytest.mark.parametrize(
    "path", ["../evil.jpg", "posters/../../evil.jpg", "/abs.jpg", "C:/x.jpg", ""]
)
def test_paths_cannot_leave_the_images_folder(fake_storage, path):
    st = fake_storage.client()
    with pytest.raises(StorageError, match="Bad image path"):
        st.save(path, JPEG)
    with pytest.raises(StorageError, match="Bad image path"):
        st.delete([path])
    assert not (fake_storage.root / "evil.jpg").exists()


def test_delete_missing_file_is_fine(fake_storage):
    fake_storage.client().delete(["posters/gone.jpg"])


# ───────── admin uploads ─────────
@pytest.fixture
def image_admin(
    db: Session, fake_storage: FakeStorage, monkeypatch
) -> Iterator[TestClient]:
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(auth_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        "app.admin.views.MediaStorage.from_settings", lambda s: fake_storage.client()
    )
    api = FastAPI()
    settings = Settings(
        _env_file=None,
        admin_password=PASSWORD,
        admin_secret_key="k" * 32,
        media_root=fake_storage.root,
        media_public_url=BASE,
    )
    setup_admin(api, db.get_bind(), settings)
    with TestClient(api, base_url="http://testserver") as c:
        c.post("/admin/login", data={"username": "admin", "password": PASSWORD})
        yield c


def get_title(db, slug):
    db.expire_all()
    return db.scalar(select(Title).where(Title.slug == slug))


def test_upload_poster_on_edit(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    old_poster = t.poster_url
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("poster.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    t = get_title(db, "hollow-tide")
    assert t.poster_url.startswith(f"{PREFIX}posters/")
    assert t.backdrop_url != t.poster_url  # only the uploaded field changed
    assert fake_storage.objects  # stored
    assert old_poster != t.poster_url


def bucket_url(path: str) -> str:
    return PREFIX + path


def test_replacing_an_image_deletes_it_only_when_nothing_else_uses_it(
    image_admin, db, fake_storage
):
    shared = bucket_url("stills/deep-cave.jpg")
    t = get_title(db, "hollow-tide")
    other = get_title(db, OTHER_SLUG)
    t.poster_url = other.poster_url = shared  # a demo still used by two titles
    db.commit()
    image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("a.jpg", JPEG, "image/jpeg")},
    )
    assert fake_storage.deleted == [], "still used by another title"

    t = get_title(db, "hollow-tide")
    first_upload = fake_storage.client().path_from_url(t.poster_url)
    image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("b.png", PNG, "image/png")},
    )
    assert fake_storage.deleted == [first_upload]


def multipart_with_empty_files(fields: dict, file_fields: list[str]) -> dict:
    """A form exactly as a browser sends it when no file was chosen (filename="").

    httpx drops empty filenames, so the body is built by hand.
    """
    b = "reelmboundary"
    parts = [
        f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
        for k, v in fields.items()
    ]
    parts += [
        f'--{b}\r\nContent-Disposition: form-data; name="{k}"; filename=""\r\n'
        "Content-Type: application/octet-stream\r\n\r\n\r\n"
        for k in file_fields
    ]
    parts.append(f"--{b}--\r\n")
    return {
        "content": "".join(parts).encode(),
        "headers": {"Content-Type": f"multipart/form-data; boundary={b}"},
    }


def episode_form(ep: Episode, **overrides) -> dict:
    data = {
        "season": str(ep.season_id),
        "number": str(ep.number),
        "name": ep.name,
        "description": ep.description,
        "release_date": ep.release_date.isoformat(),
        "duration_sec": str(ep.duration_sec),
        "thumb_url": ep.thumb_url or "",
        "intro_start_sec": str(ep.intro_start_sec),
        "intro_end_sec": str(ep.intro_end_sec),
        "video": ep.video_id,
    }
    return data | overrides


def test_browser_save_without_choosing_a_file(image_admin, db, fake_storage):
    """Regression: an empty file input on edit used to crash SQLAdmin (500)."""
    t = get_title(db, "hollow-tide")
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        **multipart_with_empty_files(
            title_form(t, name="Hollow Tide Renamed"), ["poster_file", "backdrop_file"]
        ),
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    assert get_title(db, "hollow-tide").name == "Hollow Tide Renamed"

    ep = db.get(Episode, "hollow-tide-s1e1")
    r = image_admin.post(
        f"/admin/episode/edit/{ep.id}",
        **multipart_with_empty_files(episode_form(ep, name="Renamed"), ["thumb_file"]),
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    db.expire_all()
    assert db.get(Episode, "hollow-tide-s1e1").name == "Renamed"
    assert fake_storage.objects == {}


def test_create_with_upload(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    form = title_form(t, name="Brand New", slug="brand-new")
    form.pop("video", None)  # a video belongs to one title
    r = image_admin.post(
        "/admin/title/create",
        data=form,
        files={"poster_file": ("p.jpg", JPEG, "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    new = get_title(db, "brand-new")
    assert fake_storage.client().path_from_url(new.poster_url) in fake_storage.objects
    assert fake_storage.deleted == []


def test_failed_second_upload_removes_the_first(image_admin, db, fake_storage):
    fake_storage.fail_folder = "backdrops/"
    t = get_title(db, "hollow-tide")
    before = (t.poster_url, t.backdrop_url)
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={
            "poster_file": ("p.jpg", JPEG, "image/jpeg"),
            "backdrop_file": ("b.jpg", JPEG, "image/jpeg"),
        },
        follow_redirects=False,
    )
    assert r.status_code == 400
    t = get_title(db, "hollow-tide")
    assert (t.poster_url, t.backdrop_url) == before
    assert fake_storage.objects == {}, "the poster that did upload was removed again"


def test_failed_save_removes_the_new_upload(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, slug=OTHER_SLUG),  # taken -> the save is refused
        files={"poster_file": ("p.jpg", JPEG, "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 400 and "already used" in r.text
    assert fake_storage.objects == {}


def test_image_urls_from_other_sites_are_refused(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, poster_url="https://imgur.example/cat.jpg"),
        follow_redirects=False,
    )
    assert r.status_code == 400 and "other websites" in r.text
    for ok in ("/images/stills/deep-cave.jpg", bucket_url("posters/x.jpg")):
        r = image_admin.post(
            f"/admin/title/edit/{t.id}",
            data=title_form(get_title(db, "hollow-tide"), poster_url=ok),
            follow_redirects=False,
        )
        assert r.status_code in (302, 303), (ok, r.text[:300])


def test_deleting_a_title_deletes_its_images(image_admin, db, fake_storage):
    st = fake_storage.client()
    poster = st.upload("posters", JPEG)
    thumb = st.upload("thumbnails", JPEG)
    shared = bucket_url("stills/deep-cave.jpg")
    get_title(db, OTHER_SLUG).backdrop_url = shared  # used elsewhere too
    db.commit()
    t = get_title(db, "hollow-tide")
    t.poster_url, t.backdrop_url = poster, shared
    db.get(Episode, "hollow-tide-s1e1").thumb_url = thumb
    db.commit()

    r = image_admin.delete(f"/admin/title/delete?pks={t.id}")
    assert r.status_code == 200, r.text[:300]
    assert sorted(fake_storage.deleted) == sorted(
        [st.path_from_url(poster), st.path_from_url(thumb)]
    )


def test_deleting_an_episode_deletes_its_thumbnail(image_admin, db, fake_storage):
    st = fake_storage.client()
    thumb = st.upload("thumbnails", JPEG)
    db.get(Episode, "hollow-tide-s1e2").thumb_url = thumb
    db.commit()
    image_admin.delete("/admin/episode/delete?pks=hollow-tide-s1e2")
    assert fake_storage.deleted == [st.path_from_url(thumb)]


def test_bad_image_is_rejected_and_nothing_changes(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    before = t.poster_url
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t, name="Should Not Save"),
        files={"poster_file": ("evil.svg", b"<svg onload=alert(1)>", "image/svg+xml")},
        follow_redirects=False,
    )
    assert r.status_code == 400 and "Only JPEG" in r.text
    t = get_title(db, "hollow-tide")
    assert t.poster_url == before and t.name == "Hollow Tide"


def test_no_file_keeps_current_image(image_admin, db, fake_storage):
    t = get_title(db, "hollow-tide")
    before = t.poster_url
    r = image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("", b"", "application/octet-stream")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    assert (
        get_title(db, "hollow-tide").poster_url == before and fake_storage.objects == {}
    )


def test_episode_thumbnail_upload(image_admin, db, fake_storage):
    ep = db.get(Episode, "hollow-tide-s1e1")
    r = image_admin.post(
        f"/admin/episode/edit/{ep.id}",
        data=episode_form(ep),
        files={"thumb_file": ("t.jpg", JPEG, "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303), r.text[:300]
    db.expire_all()
    assert "/images/thumbnails/" in db.get(Episode, "hollow-tide-s1e1").thumb_url


def test_upload_without_storage_configured(db, monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(auth_module.asyncio, "sleep", no_wait)
    api = FastAPI()
    setup_admin(
        api,
        db.get_bind(),
        Settings(_env_file=None, admin_password=PASSWORD, admin_secret_key="k" * 32),
    )
    with TestClient(api) as c:
        c.post("/admin/login", data={"username": "admin", "password": PASSWORD})
        t = get_title(db, "hollow-tide")
        r = c.post(
            f"/admin/title/edit/{t.id}",
            data=title_form(t),
            files={"poster_file": ("a.jpg", JPEG, "image/jpeg")},
        )
        assert r.status_code == 400 and "MEDIA_ROOT" in r.text


@pytest.mark.skipif(not STILL.exists(), reason="reelm-fe stills not present")
def test_real_still_passes_validation(fake_storage):
    data = (STILL / "deep-cave.jpg").read_bytes()
    assert sniff_image_type(data) == "image/jpeg"
    fake_storage.client().upload("stills", data, name="deep-cave")
