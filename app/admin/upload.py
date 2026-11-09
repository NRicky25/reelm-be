"""Admin page: upload a video file to Mux for an episode or movie.

Flow:
1. Admin picks a title (and episode, for shows) and a file.
2. POST /admin/upload/start creates a Mux direct upload and a `videos` row
   (status "waiting") with the episode/movie as its *pending* target.
   The episode/movie keeps playing its current video meanwhile.
3. The browser PUTs the file straight to Mux (it never passes through us).
4. The page polls /admin/upload/status/<id>, which asks Mux for progress.
   When Mux says "ready", the episode/movie switches to the new video and the
   old one is deleted. (Webhooks, if configured, do the same.)
"""

import logging
from datetime import timedelta
from typing import Any, ClassVar

import anyio
from sqladmin import BaseView, expose
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import Settings
from app.models import (
    Episode,
    Season,
    Title,
    TitleType,
    Video,
    VideoProvider,
    VideoStatus,
)
from app.services.mux import MuxClient, MuxError
from app.services.video_sync import sync_from_mux, sync_stale_uploads

log = logging.getLogger(__name__)
STALE_AFTER = timedelta(minutes=2)


class UploadView(BaseView):
    name = "Upload video"
    identity = "upload"
    icon = "fa-solid fa-cloud-arrow-up"
    category = "Media"

    # set by setup_admin()
    session_maker: ClassVar[sessionmaker]
    settings: ClassVar[Settings]

    def _mux(self) -> MuxClient:
        return MuxClient.from_settings(self.settings)

    async def _db(self, fn):
        def run():
            with self.session_maker() as session:
                return fn(session)

        return await anyio.to_thread.run_sync(run)

    @expose("/upload", methods=["GET"], identity="upload")
    async def page(self, request: Request) -> Response:
        if self.settings.mux_enabled:
            # catch up on uploads that were left waiting (closed tab, no webhooks)
            def catch_up() -> None:
                mux = self._mux()
                try:
                    sync_stale_uploads(self.session_maker, mux, older_than=STALE_AFTER)
                finally:
                    mux.close()

            try:
                await anyio.to_thread.run_sync(catch_up)
            except MuxError as e:
                log.warning("Couldn't check waiting uploads: %s", e)

        titles = await self._db(
            lambda s: [
                {"id": t.id, "name": t.name, "type": t.type.value}
                for t in s.scalars(select(Title).order_by(Title.name))
            ]
        )
        return await self.templates.TemplateResponse(
            request,
            "reelm/upload.html",
            {
                "titles": titles,
                "mux_enabled": self.settings.mux_enabled,
                "title": "Upload video",
            },
        )

    @expose("/upload/episodes", methods=["GET"], identity="upload_episodes")
    async def episodes(self, request: Request) -> Response:
        title_id = request.query_params.get("title_id", "")

        def load(s: Session) -> list[dict[str, Any]]:
            rows = s.execute(
                select(Season.number, Episode)
                .join(Episode, Episode.season_id == Season.id)
                .where(Season.title_id == title_id)
                .order_by(Season.number, Episode.number)
            ).all()
            return [
                {
                    "id": ep.id,
                    "label": f"S{season} · E{ep.number} · {ep.name}",
                    "video": str(ep.video) if ep.video else None,
                }
                for season, ep in rows
            ]

        return JSONResponse(await self._db(load))

    @expose("/upload/start", methods=["POST"], identity="upload_start")
    async def start(self, request: Request) -> Response:
        if not self.settings.mux_enabled:
            return JSONResponse(
                {"error": "Mux isn't configured (MUX_TOKEN_ID / MUX_TOKEN_SECRET)."},
                400,
            )
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return JSONResponse({"error": "Send a JSON object."}, 400)
        title_id, episode_id = body.get("title_id"), body.get("episode_id") or None
        # The same-origin middleware has already checked Origin against Host.
        origin = request.headers.get("origin") or str(request.base_url).rstrip("/")

        # 1) check the target (short DB read)
        def find_target(s: Session) -> dict[str, str]:
            title = s.get(Title, title_id) if isinstance(title_id, str) else None
            if title is None:
                raise ValueError("Pick a title.")
            if title.type == TitleType.movie:
                return {"pending_title_id": title.id}
            episode = (
                s.get(Episode, episode_id) if isinstance(episode_id, str) else None
            )
            if episode is None or episode.season.title_id != title.id:
                raise ValueError("Pick an episode of this show.")
            return {"pending_episode_id": episode.id}

        # 2) create the Mux upload (no transaction open)
        def create_upload() -> dict[str, Any]:
            mux = self._mux()
            try:
                return mux.create_upload(cors_origin=origin)
            finally:
                mux.close()

        # 3) remember it, with its pending target
        def save(s: Session, target: dict[str, str], upload: dict[str, Any]) -> str:
            video = Video(
                provider=VideoProvider.mux,
                status=VideoStatus.waiting,
                upload_id=upload["id"],
                **target,
            )
            s.add(video)
            s.commit()
            return video.id

        try:
            target = await self._db(find_target)
            upload = await anyio.to_thread.run_sync(create_upload)
            video_id = await self._db(lambda s: save(s, target, upload))
        except (ValueError, MuxError) as e:
            return JSONResponse({"error": str(e)}, 400)
        return JSONResponse({"video_id": video_id, "upload_url": upload["url"]})

    @expose("/upload/status/{video_id}", methods=["GET"], identity="upload_status")
    async def status(self, request: Request) -> Response:
        video_id = request.path_params["video_id"]

        def check() -> dict[str, Any] | None:
            mux = self._mux()
            try:
                return sync_from_mux(self.session_maker, mux, video_id)
            finally:
                mux.close()

        try:
            result = await anyio.to_thread.run_sync(check)
        except MuxError as e:
            return JSONResponse({"status": "waiting", "error": None, "note": str(e)})
        if result is None:
            return JSONResponse(
                {"error": "Unknown video (it may have been replaced or deleted)."}, 404
            )
        return JSONResponse(result)
