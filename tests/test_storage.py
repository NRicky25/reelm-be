"""Image storage: the Supabase Storage client and admin image uploads (with a fake Supabase)."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
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
    StorageError,
    SupabaseStorage,
    sniff_image_type,
)
from tests.test_admin_hardening import title_form

PASSWORD = "test-password-123"
BASE = "https://proj.supabase.co"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100
STILL = (
    Path(__file__).resolve().parents[2] / "reelm-fe" / "public" / "images" / "stills"
)


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.buckets: dict[str, dict] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        assert request.headers["apikey"] == "service-key"
        if method == "GET" and path.startswith("/storage/v1/bucket/"):
            name = path.rsplit("/", 1)[1]
            if name in self.buckets:
                return httpx.Response(200, json=self.buckets[name])
            return httpx.Response(400, json={"message": "Bucket not found"})
        if method == "POST" and path == "/storage/v1/bucket":
            body = json.loads(request.content)
            self.buckets[body["name"]] = body
            return httpx.Response(200, json={"name": body["name"]})
        prefix = "/storage/v1/object/images/"
        if method == "POST" and path.startswith(prefix):
            self.objects[path[len(prefix) :]] = request.content
            return httpx.Response(200, json={"Key": path})
        if method == "DELETE" and path == "/storage/v1/object/images":
            for p in json.loads(request.content)["prefixes"]:
                self.deleted.append(p)
                self.objects.pop(p, None)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "no route"})

    def client(self) -> SupabaseStorage:
        return SupabaseStorage(
            BASE, "service-key", "images", transport=httpx.MockTransport(self.handler)
        )


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


# ───────── client ─────────
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
    assert url.startswith(
        f"{BASE}/storage/v1/object/public/images/posters/"
    ) and url.endswith(".jpg")
    assert fake_storage.objects[path] == JPEG
    assert st.path_from_url("https://elsewhere.example/x.jpg") is None
    assert st.upload("stills", PNG, name="fixed").endswith("/stills/fixed.png")


def test_upload_rejects_bad_files(fake_storage):
    st = fake_storage.client()
    with pytest.raises(StorageError, match="Only JPEG"):
        st.upload("posters", b"<html>")
    with pytest.raises(StorageError, match="too large"):
        st.upload("posters", JPEG + b"0" * MAX_BYTES)
    assert fake_storage.objects == {}


def test_bucket_is_created_once_and_public(fake_storage):
    st = fake_storage.client()
    st.ensure_bucket()
    st.ensure_bucket()
    assert fake_storage.buckets["images"]["public"] is True
    assert set(fake_storage.buckets["images"]["allowed_mime_types"]) == {
        "image/jpeg",
        "image/png",
        "image/webp",
    }


# ───────── admin uploads ─────────
@pytest.fixture
def image_admin(
    db: Session, fake_storage: FakeStorage, monkeypatch
) -> Iterator[TestClient]:
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(auth_module.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        "app.admin.views.SupabaseStorage.from_settings", lambda s: fake_storage.client()
    )
    api = FastAPI()
    settings = Settings(
        _env_file=None,
        admin_password=PASSWORD,
        admin_secret_key="k" * 32,
        supabase_url=BASE,
        supabase_service_role_key="service-key",
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
    assert t.poster_url.startswith(f"{BASE}/storage/v1/object/public/images/posters/")
    assert t.backdrop_url != t.poster_url  # only the uploaded field changed
    assert fake_storage.objects  # stored
    assert old_poster != t.poster_url


def test_replacing_an_uploaded_image_deletes_the_old_one_but_not_shared_stills(
    image_admin, db, fake_storage
):
    t = get_title(db, "hollow-tide")
    t.poster_url = f"{BASE}/storage/v1/object/public/images/stills/deep-cave.jpg"  # shared demo still
    db.commit()
    image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("a.jpg", JPEG, "image/jpeg")},
    )
    assert fake_storage.deleted == [], "shared stills are never deleted"

    t = get_title(db, "hollow-tide")
    first_upload = fake_storage.client().path_from_url(t.poster_url)
    image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("b.png", PNG, "image/png")},
    )
    assert fake_storage.deleted == [first_upload]


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
    image_admin.post(
        f"/admin/title/edit/{t.id}",
        data=title_form(t),
        files={"poster_file": ("", b"", "application/octet-stream")},
    )
    assert (
        get_title(db, "hollow-tide").poster_url == before and fake_storage.objects == {}
    )


def test_episode_thumbnail_upload(image_admin, db, fake_storage):
    ep = db.get(Episode, "hollow-tide-s1e1")
    r = image_admin.post(
        f"/admin/episode/edit/{ep.id}",
        data={
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
        },
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
        assert r.status_code == 400 and "SUPABASE_URL" in r.text


@pytest.mark.skipif(not STILL.exists(), reason="reelm-fe stills not present")
def test_real_still_passes_validation(fake_storage):
    data = (STILL / "deep-cave.jpg").read_bytes()
    assert sniff_image_type(data) == "image/jpeg"
    fake_storage.client().upload("stills", data, name="deep-cave")
