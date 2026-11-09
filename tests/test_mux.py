"""Mux integration: API client, webhook signatures, status syncing, admin upload page.

Mux is faked with httpx.MockTransport, so these tests never call the real API.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.admin.auth as auth_module
from app.admin import setup_admin
from app.admin.upload import UploadView
from app.config import Settings, get_settings
from app.models import Episode, Title, Video, VideoProvider, VideoStatus
from app.services.mux import MuxClient, MuxError, verify_webhook
from app.services.video_sync import handle_webhook_event, sync_from_mux
from app.services.video_url import playback_url

PASSWORD = "test-password-123"


class FakeMux:
    """Just enough of the Mux API: uploads -> assets -> ready."""

    def __init__(self) -> None:
        self.uploads: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if method == "POST" and path == "/video/v1/uploads":
            body = json.loads(request.content)
            upload_id = f"up{len(self.uploads) + 1}"
            self.uploads[upload_id] = {
                "id": upload_id,
                "status": "waiting",
                "url": f"https://upload.example/{upload_id}",
                "cors_origin": body["cors_origin"],
                "new_asset_settings": body["new_asset_settings"],
            }
            return httpx.Response(201, json={"data": self.uploads[upload_id]})
        if method == "GET" and path.startswith("/video/v1/uploads/"):
            return httpx.Response(
                200, json={"data": self.uploads[path.rsplit("/", 1)[1]]}
            )
        if method == "GET" and path.startswith("/video/v1/assets/"):
            asset_id = path.rsplit("/", 1)[1]
            if asset_id not in self.assets:
                return httpx.Response(404, json={"error": {"messages": ["not found"]}})
            return httpx.Response(200, json={"data": self.assets[asset_id]})
        if method == "DELETE" and path.startswith("/video/v1/assets/"):
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"messages": ["no route"]}})

    # simulate Mux receiving the file, then finishing processing
    def finish_upload(self, upload_id: str) -> str:
        asset_id = f"asset-{upload_id}"
        self.uploads[upload_id].update(status="asset_created", asset_id=asset_id)
        self.assets[asset_id] = {
            "id": asset_id,
            "status": "preparing",
            "upload_id": upload_id,
        }
        return asset_id

    def make_ready(self, asset_id: str, duration: float = 1432.5) -> None:
        self.assets[asset_id].update(
            status="ready",
            duration=duration,
            playback_ids=[{"id": f"pb-{asset_id}", "policy": "public"}],
        )

    def client(self) -> MuxClient:
        return MuxClient("id", "secret", transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake_mux() -> FakeMux:
    return FakeMux()


def sign(body: bytes, secret: str, at: int | None = None) -> str:
    t = at if at is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


# ───────── client ─────────
def test_client_creates_public_upload_with_cors(fake_mux):
    up = fake_mux.client().create_upload(cors_origin="http://localhost:8000")
    assert up["url"].startswith("https://upload.example/")
    sent = json.loads(fake_mux.requests[0].content)
    assert sent["cors_origin"] == "http://localhost:8000"
    assert sent["new_asset_settings"]["playback_policies"] == ["public"]
    assert fake_mux.requests[0].headers["authorization"].startswith("Basic ")


def test_client_errors_are_mux_errors(fake_mux):
    with pytest.raises(MuxError, match="404"):
        fake_mux.client().get_asset("missing")
    fake_mux.client().delete_asset("missing")  # deleting something already gone is fine


def test_client_needs_configuration():
    with pytest.raises(MuxError, match="isn't configured"):
        MuxClient.from_settings(Settings(_env_file=None))


# ───────── webhook signatures ─────────
def test_webhook_signature():
    body = b'{"type":"video.asset.ready"}'
    assert verify_webhook(body, sign(body, "s3cret"), "s3cret")
    assert not verify_webhook(body, sign(body, "wrong"), "s3cret")
    assert not verify_webhook(
        body + b" ", sign(body, "s3cret"), "s3cret"
    )  # body changed
    assert not verify_webhook(
        body, sign(body, "s3cret", at=int(time.time()) - 3600), "s3cret"
    )  # replay
    assert not verify_webhook(body, None, "s3cret")
    assert not verify_webhook(body, "garbage", "s3cret")


# ───────── syncing ─────────
def mux_video_on(db: Session, episode_id: str, upload_id: str = "up1") -> Video:
    video = Video(
        provider=VideoProvider.mux, status=VideoStatus.waiting, upload_id=upload_id
    )
    ep = db.get(Episode, episode_id)
    ep.video = video
    ep.duration_sec = 0
    db.commit()
    return video


def test_polling_follows_upload_to_ready(db, fake_mux):
    fake_mux.uploads["up1"] = {"id": "up1", "status": "waiting"}
    video = mux_video_on(db, "hollow-tide-s1e1")
    mux = fake_mux.client()

    sync_from_mux(db, mux, video)
    assert video.status == VideoStatus.waiting and video.asset_id is None
    assert playback_url(video) is None

    asset_id = fake_mux.finish_upload("up1")
    sync_from_mux(db, mux, video)
    assert video.asset_id == asset_id and video.status == VideoStatus.waiting

    fake_mux.make_ready(asset_id)
    sync_from_mux(db, mux, video)
    assert video.status == VideoStatus.ready
    assert playback_url(video) == f"https://stream.mux.com/pb-{asset_id}.m3u8"
    assert (
        db.get(Episode, "hollow-tide-s1e1").duration_sec == 1432
    )  # filled in from Mux


def test_failed_upload_marks_error(db, fake_mux):
    fake_mux.uploads["up1"] = {
        "id": "up1",
        "status": "errored",
        "error": {"message": "bad file"},
    }
    video = mux_video_on(db, "hollow-tide-s1e1")
    sync_from_mux(db, fake_mux.client(), video)
    assert video.status == VideoStatus.errored and video.error_message == "bad file"


def test_webhook_events_update_video(db):
    video = mux_video_on(db, "hollow-tide-s1e1", upload_id="upX")
    assert handle_webhook_event(
        db,
        {"type": "video.upload.asset_created", "data": {"id": "upX", "asset_id": "aX"}},
    ).startswith("applied")
    assert video.asset_id == "aX"
    note = handle_webhook_event(
        db,
        {
            "type": "video.asset.ready",
            "data": {
                "id": "aX",
                "upload_id": "upX",
                "status": "ready",
                "duration": 60.2,
                "playback_ids": [{"id": "pbX", "policy": "public"}],
            },
        },
    )
    assert note.startswith("applied")
    assert video.status == VideoStatus.ready and video.playback_id == "pbX"
    assert handle_webhook_event(
        db, {"type": "video.asset.ready", "data": {"id": "nope"}}
    ).startswith("ignored")
    assert handle_webhook_event(db, {"type": "something.else"}).startswith("ignored")


# ───────── webhook endpoint ─────────
@pytest.fixture
def webhook_secret(monkeypatch) -> Iterator[str]:
    monkeypatch.setenv("MUX_WEBHOOK_SECRET", "whsec_test")
    get_settings.cache_clear()
    yield "whsec_test"
    get_settings.cache_clear()


def test_webhook_endpoint_rejects_bad_signature(client, webhook_secret):
    body = json.dumps({"type": "video.asset.ready", "data": {"id": "x"}}).encode()
    r = client.post(
        "/webhooks/mux", content=body, headers={"Mux-Signature": sign(body, "wrong")}
    )
    assert r.status_code == 401


def test_webhook_endpoint_applies_signed_event(client, db, webhook_secret):
    video = mux_video_on(db, "hollow-tide-s1e2", upload_id="upW")
    body = json.dumps(
        {"type": "video.upload.asset_created", "data": {"id": "upW", "asset_id": "aW"}}
    ).encode()
    r = client.post(
        "/webhooks/mux",
        content=body,
        headers={"Mux-Signature": sign(body, webhook_secret)},
    )
    assert r.status_code == 204
    db.refresh(video)
    assert video.asset_id == "aW"


def test_webhook_endpoint_off_without_secret(client, monkeypatch):
    monkeypatch.delenv("MUX_WEBHOOK_SECRET", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "app.routers.webhooks.get_settings", lambda: Settings(_env_file=None)
    )
    assert client.post("/webhooks/mux", content=b"{}").status_code == 503
    get_settings.cache_clear()


# ───────── admin upload page ─────────
@pytest.fixture
def upload_admin(db: Session, fake_mux: FakeMux, monkeypatch) -> Iterator[TestClient]:
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(auth_module.asyncio, "sleep", no_wait)
    api = FastAPI()
    settings = Settings(
        _env_file=None,
        admin_password=PASSWORD,
        admin_secret_key="k" * 32,
        mux_token_id="id",
        mux_token_secret="secret",
    )
    setup_admin(api, db.get_bind(), settings)
    monkeypatch.setattr(UploadView, "_mux", lambda self: fake_mux.client())
    monkeypatch.setattr(
        "app.admin.views.MuxClient.from_settings", lambda s: fake_mux.client()
    )
    with TestClient(api, base_url="http://testserver") as c:
        c.post("/admin/login", data={"username": "admin", "password": PASSWORD})
        yield c


def test_upload_page_loads(upload_admin):
    r = upload_admin.get("/admin/upload")
    assert r.status_code == 200 and "Upload a video to Mux" in r.text
    eps = upload_admin.get("/admin/upload/episodes", params={"title_id": "t05"}).json()
    assert eps[0]["label"].startswith("S1 · E1")


def test_upload_flow_for_an_episode(upload_admin, db, fake_mux):
    old_video_id = db.get(Episode, "hollow-tide-s1e1").video_id
    r = upload_admin.post(
        "/admin/upload/start",
        json={"title_id": "t05", "episode_id": "hollow-tide-s1e1"},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["upload_url"].startswith("https://upload.example/")
    assert fake_mux.uploads["up1"]["cors_origin"] == "http://testserver"

    db.expire_all()
    ep = db.get(Episode, "hollow-tide-s1e1")
    assert ep.video_id == start["video_id"]
    assert db.get(Video, old_video_id) is None  # the replaced video is removed

    # not ready yet: the episode isn't playable
    assert (
        upload_admin.get(f"/admin/upload/status/{start['video_id']}").json()["status"]
        == "waiting"
    )
    fake_mux.make_ready(fake_mux.finish_upload("up1"))
    status = upload_admin.get(f"/admin/upload/status/{start['video_id']}").json()
    assert status["status"] == "ready" and status["playback_id"] == "pb-asset-up1"


def test_upload_flow_for_a_movie(upload_admin, db, fake_mux):
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t20"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Title, "t20").video_id == r.json()["video_id"]


def test_upload_needs_a_valid_target(upload_admin):
    assert (
        upload_admin.post("/admin/upload/start", json={"title_id": "nope"}).status_code
        == 400
    )
    # a show without an episode chosen
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t05"})
    assert r.status_code == 400 and "episode" in r.json()["error"]
    # an episode from another show
    r = upload_admin.post(
        "/admin/upload/start",
        json={"title_id": "t05", "episode_id": "skyward-ronin-s1e1"},
    )
    assert r.status_code == 400


def test_upload_endpoints_need_login(db, fake_mux):
    api = FastAPI()
    setup_admin(
        api,
        db.get_bind(),
        Settings(
            _env_file=None,
            admin_password=PASSWORD,
            mux_token_id="i",
            mux_token_secret="s",
        ),
    )
    c = TestClient(api)
    for method, url in [
        ("GET", "/admin/upload"),
        ("POST", "/admin/upload/start"),
        ("GET", "/admin/upload/status/x"),
    ]:
        r = c.request(method, url, follow_redirects=False)
        assert r.status_code in (302, 303), url
    assert fake_mux.requests == []


def test_deleting_a_mux_video_deletes_the_asset(upload_admin, db, fake_mux):
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t20"})
    video_id = r.json()["video_id"]
    fake_mux.make_ready(fake_mux.finish_upload("up1"))
    upload_admin.get(f"/admin/upload/status/{video_id}")
    upload_admin.delete("/admin/title/delete?pks=t20")
    assert "asset-up1" in fake_mux.deleted
