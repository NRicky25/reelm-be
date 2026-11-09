"""Mux integration: API client, webhook signatures, status syncing, admin upload page.

Mux is faked with httpx.MockTransport, so these tests never call the real API.
"""

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import app.admin.auth as auth_module
from app.admin import setup_admin
from app.admin.upload import UploadView
from app.config import Settings, get_settings
from app.models import Episode, Title, Video, VideoProvider, VideoStatus
from app.services.mux import MuxClient, MuxError, verify_webhook
from app.services.video_sync import (
    handle_webhook_event,
    sync_from_mux,
    sync_stale_uploads,
)
from app.services.video_url import playback_url

PASSWORD = "test-password-123"
EP = "hollow-tide-s1e1"


class FakeMux:
    """Just enough of the Mux API: uploads -> assets -> ready."""

    def __init__(self) -> None:
        self.uploads: dict[str, dict] = {}
        self.assets: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
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
                "timeout": body.get("timeout"),
                "new_asset_settings": body["new_asset_settings"],
            }
            return httpx.Response(201, json={"data": self.uploads[upload_id]})
        if method == "PUT" and path.endswith("/cancel"):
            upload_id = path.split("/")[-2]
            up = self.uploads[upload_id]
            if up["status"] != "waiting":
                return httpx.Response(
                    400, json={"error": {"messages": ["not waiting"]}}
                )
            up["status"] = "cancelled"
            self.cancelled.append(upload_id)
            return httpx.Response(200, json={"data": up})
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


@pytest.fixture
def sessions(db: Session) -> sessionmaker:
    return sessionmaker(bind=db.get_bind(), expire_on_commit=False)


def sign(body: bytes, secret: str, at: int | None = None) -> str:
    t = at if at is not None else int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def pending_upload(
    db: Session, upload_id: str = "up1", *, episode_id: str = EP
) -> Video:
    video = Video(
        provider=VideoProvider.mux,
        status=VideoStatus.waiting,
        upload_id=upload_id,
        pending_episode_id=episode_id,
    )
    db.add(video)
    db.commit()
    return video


# ───────── client ─────────
def test_client_creates_public_upload_with_cors_and_timeout(fake_mux):
    up = fake_mux.client().create_upload(cors_origin="http://localhost:8000")
    assert up["url"].startswith("https://upload.example/")
    sent = json.loads(fake_mux.requests[0].content)
    assert sent["cors_origin"] == "http://localhost:8000"
    assert sent["timeout"] == 3600
    assert sent["new_asset_settings"]["playback_policies"] == ["public"]
    assert fake_mux.requests[0].headers["authorization"].startswith("Basic ")


def test_client_errors_carry_status(fake_mux):
    with pytest.raises(MuxError) as e:
        fake_mux.client().get_asset("missing")
    assert e.value.status_code == 404
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
    old = int(time.time()) - 3600
    assert not verify_webhook(body, sign(body, "s3cret", at=old), "s3cret")  # replay
    assert not verify_webhook(body, None, "s3cret")
    assert not verify_webhook(body, "garbage", "s3cret")
    # Mux may send several v1 signatures (e.g. during secret rotation)
    good = sign(body, "s3cret")
    assert verify_webhook(body, good.replace("v1=", "v1=deadbeef,v1="), "s3cret")


# ───────── swapping in a new video only when it's ready ─────────
def test_polling_follows_upload_to_ready_then_swaps(db, sessions, fake_mux):
    fake_mux.uploads["up1"] = {"id": "up1", "status": "waiting"}
    old_video_id = db.get(Episode, EP).video_id
    video = pending_upload(db)
    mux = fake_mux.client()

    assert sync_from_mux(sessions, mux, video.id)["status"] == "waiting"
    db.expire_all()
    assert db.get(Episode, EP).video_id == old_video_id, (
        "keeps playing the old video meanwhile"
    )

    asset_id = fake_mux.finish_upload("up1")
    assert sync_from_mux(sessions, mux, video.id)["status"] == "waiting"

    fake_mux.make_ready(asset_id)
    result = sync_from_mux(sessions, mux, video.id)
    assert result["status"] == "ready"
    db.expire_all()
    ep = db.get(Episode, EP)
    assert ep.video_id == video.id
    assert playback_url(ep.video) == f"https://stream.mux.com/pb-{asset_id}.m3u8"
    assert ep.duration_sec == 1432  # updated to the new video's length
    assert db.get(Video, old_video_id) is None, "replaced video removed after the swap"


def test_failed_upload_leaves_episode_untouched(db, sessions, fake_mux):
    fake_mux.uploads["up1"] = {
        "id": "up1",
        "status": "errored",
        "error": {"message": "bad file"},
    }
    old_video_id = db.get(Episode, EP).video_id
    video = pending_upload(db)
    result = sync_from_mux(sessions, fake_mux.client(), video.id)
    assert result["status"] == "errored" and result["error"] == "bad file"
    db.expire_all()
    assert db.get(Episode, EP).video_id == old_video_id
    assert db.get(Video, old_video_id) is not None


def test_replacing_a_mux_video_deletes_old_asset(db, sessions, fake_mux):
    mux = fake_mux.client()
    for upload_id in ("up1", "up2"):
        fake_mux.uploads[upload_id] = {"id": upload_id, "status": "waiting"}
        video = pending_upload(db, upload_id)
        fake_mux.make_ready(fake_mux.finish_upload(upload_id))
        sync_from_mux(sessions, mux, video.id)
    assert fake_mux.deleted == ["asset-up1"]


def test_stale_uploads_are_caught_up(db, sessions, fake_mux):
    fake_mux.uploads["up1"] = {"id": "up1", "status": "timed_out"}
    video = pending_upload(db)
    video.created_at = datetime.now(UTC) - timedelta(hours=2)
    db.commit()
    assert (
        sync_stale_uploads(sessions, fake_mux.client(), older_than=timedelta(minutes=2))
        == 1
    )
    db.expire_all()
    assert db.get(Video, video.id).status == VideoStatus.errored


# ───────── webhooks ─────────
def test_webhook_events_swap_and_ignore_unknowns(db):
    video = pending_upload(db, "upX")
    assert handle_webhook_event(
        db,
        {"type": "video.upload.asset_created", "data": {"id": "upX", "asset_id": "aX"}},
    ).startswith("applied")
    assert video.asset_id == "aX"
    ready = {
        "type": "video.asset.ready",
        "data": {
            "id": "aX",
            "upload_id": "upX",
            "status": "ready",
            "duration": 60.2,
            "playback_ids": [{"id": "pbX", "policy": "public"}],
        },
    }
    assert handle_webhook_event(db, ready).startswith("applied")
    assert video.status == VideoStatus.ready and video.playback_id == "pbX"
    assert db.get(Episode, EP).video_id == video.id
    # a late "created" (status preparing) must not undo "ready"
    late = {"type": "video.asset.created", "data": {"id": "aX", "status": "preparing"}}
    handle_webhook_event(db, late)
    assert video.status == VideoStatus.ready
    assert handle_webhook_event(
        db, {"type": "video.asset.ready", "data": {"id": "?"}}
    ).startswith("ignored")
    assert handle_webhook_event(db, {"type": "something.else"}).startswith("ignored")


def test_asset_deleted_in_mux_dashboard(db):
    video = pending_upload(db, "upD")
    video.asset_id, video.status, video.playback_id = "aD", VideoStatus.ready, "pbD"
    db.commit()
    handle_webhook_event(db, {"type": "video.asset.deleted", "data": {"id": "aD"}})
    assert video.status == VideoStatus.errored and playback_url(video) is None


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
    video = pending_upload(db, "upW", episode_id="hollow-tide-s1e2")
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
    # signed but not a JSON object
    body = b"[1,2]"
    r = client.post(
        "/webhooks/mux",
        content=body,
        headers={"Mux-Signature": sign(body, webhook_secret)},
    )
    assert r.status_code == 400


def test_webhook_endpoint_off_without_secret(client, monkeypatch):
    monkeypatch.setattr(
        "app.routers.webhooks.get_settings", lambda: Settings(_env_file=None)
    )
    assert client.post("/webhooks/mux", content=b"{}").status_code == 503


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
    old_video_id = db.get(Episode, EP).video_id
    r = upload_admin.post(
        "/admin/upload/start",
        json={"title_id": "t05", "episode_id": EP},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["upload_url"].startswith("https://upload.example/")
    assert fake_mux.uploads["up1"]["cors_origin"] == "http://testserver"

    db.expire_all()
    assert db.get(Episode, EP).video_id == old_video_id, (
        "old video still plays during upload"
    )
    status = upload_admin.get(f"/admin/upload/status/{start['video_id']}").json()
    assert status["status"] == "waiting"

    fake_mux.make_ready(fake_mux.finish_upload("up1"))
    status = upload_admin.get(f"/admin/upload/status/{start['video_id']}").json()
    assert status["status"] == "ready" and status["playback_id"] == "pb-asset-up1"
    db.expire_all()
    assert db.get(Episode, EP).video_id == start["video_id"]
    assert db.get(Video, old_video_id) is None


def test_upload_flow_for_a_movie(upload_admin, db, fake_mux):
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t20"})
    assert r.status_code == 200, r.text
    video_id = r.json()["video_id"]
    fake_mux.make_ready(fake_mux.finish_upload("up1"))
    upload_admin.get(f"/admin/upload/status/{video_id}")
    db.expire_all()
    assert db.get(Title, "t20").video_id == video_id


def test_upload_needs_a_valid_target(upload_admin):
    assert (
        upload_admin.post("/admin/upload/start", json={"title_id": "nope"}).status_code
        == 400
    )
    r = upload_admin.post(
        "/admin/upload/start", json={"title_id": "t05"}
    )  # show, no episode
    assert r.status_code == 400 and "episode" in r.json()["error"]
    r = upload_admin.post(  # episode from another show
        "/admin/upload/start",
        json={"title_id": "t05", "episode_id": "skyward-ronin-s1e1"},
    )
    assert r.status_code == 400
    assert upload_admin.post("/admin/upload/start", json=[1, 2]).status_code == 400
    r = upload_admin.post(
        "/admin/upload/start",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_upload_start_blocks_other_origins(upload_admin, fake_mux):
    r = upload_admin.post(
        "/admin/upload/start",
        json={"title_id": "t20"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403 and fake_mux.uploads == {}


def test_status_of_unknown_video(upload_admin):
    assert upload_admin.get("/admin/upload/status/nope").status_code == 404


def test_upload_endpoints_need_login(db, fake_mux):
    api = FastAPI()
    settings = Settings(
        _env_file=None, admin_password=PASSWORD, mux_token_id="i", mux_token_secret="s"
    )
    setup_admin(api, db.get_bind(), settings)
    c = TestClient(api)
    for method, url in [
        ("GET", "/admin/upload"),
        ("POST", "/admin/upload/start"),
        ("GET", "/admin/upload/status/x"),
    ]:
        assert c.request(method, url, follow_redirects=False).status_code in (
            302,
            303,
        ), url
    assert fake_mux.requests == []


def test_deleting_a_mux_video_deletes_the_asset(upload_admin, db, fake_mux):
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t20"})
    video_id = r.json()["video_id"]
    fake_mux.make_ready(fake_mux.finish_upload("up1"))
    upload_admin.get(f"/admin/upload/status/{video_id}")
    upload_admin.delete("/admin/title/delete?pks=t20")
    assert "asset-up1" in fake_mux.deleted


def test_deleting_a_waiting_upload_cancels_it(upload_admin, db, fake_mux):
    r = upload_admin.post(
        "/admin/upload/start", json={"title_id": "t05", "episode_id": EP}
    )
    assert r.status_code == 200
    # the episode is deleted while its new video is still uploading
    upload_admin.delete(f"/admin/episode/delete?pks={EP}")
    assert fake_mux.cancelled == ["up1"]
    db.expire_all()
    assert db.get(Video, r.json()["video_id"]) is None


def test_deleting_upload_that_already_made_an_asset(upload_admin, db, fake_mux):
    r = upload_admin.post("/admin/upload/start", json={"title_id": "t20"})
    fake_mux.finish_upload("up1")  # Mux created the asset, but we never synced
    upload_admin.delete(f"/admin/video/delete?pks={r.json()['video_id']}")
    assert "asset-up1" in fake_mux.deleted
