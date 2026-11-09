"""Admin page: upload a video file to Mux for an episode or movie.

Flow:
1. Admin picks a title (and episode, for shows) and a file.
2. POST /admin/upload/start creates a Mux direct upload + a `videos` row
   (status "waiting") and attaches it to the episode/movie.
3. The browser PUTs the file straight to Mux (it never passes through us).
4. The page polls /admin/upload/status/<id>, which asks Mux for progress,
   until the video is "ready". (Webhooks, if configured, update it too.)
"""

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
from app.services.video_sync import sync_from_mux


def delete_videos_everywhere(
    session: Session, video_ids: list[str], mux: MuxClient | None
) -> None:
    """Delete video rows and, for Mux videos, the Mux asset too (best effort)."""
    if not video_ids:
        return
    videos = list(session.scalars(select(Video).where(Video.id.in_(video_ids))))
    for v in videos:
        if mux and v.provider == VideoProvider.mux and v.asset_id:
            try:
                mux.delete_asset(v.asset_id)
            except MuxError:
                pass  # the row still goes; an orphan asset can be removed in the Mux dashboard
        session.delete(v)
    session.commit()


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
        body = await request.json()
        title_id, episode_id = body.get("title_id"), body.get("episode_id") or None
        origin = request.headers.get("origin") or str(request.base_url).rstrip("/")

        def start_upload(s: Session) -> dict[str, Any]:
            title = s.get(Title, title_id)
            if title is None:
                raise ValueError("Pick a title.")
            if title.type == TitleType.movie:
                owner: Title | Episode = title
            else:
                episode = s.get(Episode, episode_id) if episode_id else None
                if episode is None or episode.season.title_id != title.id:
                    raise ValueError("Pick an episode of this show.")
                owner = episode

            mux = self._mux()
            try:
                upload = mux.create_upload(cors_origin=origin)
                video = Video(
                    provider=VideoProvider.mux,
                    status=VideoStatus.waiting,
                    upload_id=upload["id"],
                )
                s.add(video)
                old_video_id = owner.video_id
                owner.video = video
                s.commit()
                # the old video (if any) is replaced: remove it, including its Mux asset
                delete_videos_everywhere(s, [old_video_id] if old_video_id else [], mux)
                return {"video_id": video.id, "upload_url": upload["url"]}
            finally:
                mux.close()

        try:
            return JSONResponse(await self._db(start_upload))
        except (ValueError, MuxError) as e:
            return JSONResponse({"error": str(e)}, 400)

    @expose("/upload/status/{video_id}", methods=["GET"], identity="upload_status")
    async def status(self, request: Request) -> Response:
        video_id = request.path_params["video_id"]

        def check(s: Session) -> dict[str, Any] | None:
            video = s.get(Video, video_id)
            if video is None:
                return None
            if (
                video.provider == VideoProvider.mux
                and video.status == VideoStatus.waiting
            ):
                mux = self._mux()
                try:
                    sync_from_mux(s, mux, video)
                finally:
                    mux.close()
            return {
                "status": video.status.value,
                "error": video.error_message,
                "playback_id": video.playback_id,
                "duration_sec": video.duration_sec,
            }

        try:
            result = await self._db(check)
        except MuxError as e:
            return JSONResponse({"status": "waiting", "error": None, "note": str(e)})
        if result is None:
            return JSONResponse({"error": "Unknown video"}, 404)
        return JSONResponse(result)
