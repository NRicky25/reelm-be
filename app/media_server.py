"""Read-only file server for the self-hosted media folder (MEDIA_ROOT).

Serves <MEDIA_ROOT>/images and <MEDIA_ROOT>/videos to the internet through the
Cloudflare Tunnel (media.<domain> -> 127.0.0.1:8090). Only GET/HEAD; no folder
listings; nothing outside those two folders; videos still being encoded (their
folder has an .incomplete marker) are hidden.

    uv run uvicorn app.media_server:app --host 127.0.0.1 --port 8090
"""

import mimetypes
from pathlib import PurePosixPath

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.config import Settings, get_settings

# Windows' registry gives wrong/missing types for these (.ts -> "vnd.dlna" or
# TypeScript), and players and browsers care.
for ext, kind in {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".vtt": "text/vtt",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}.items():
    mimetypes.add_type(kind, ext)

PUBLIC_FOLDERS = {"images", "videos"}
INCOMPLETE = ".incomplete"  # same marker reelm-encoder puts in a folder while encoding
PLAYLIST_MAX_AGE = 60
FILE_MAX_AGE = 7 * 24 * 3600


class MediaFiles(StaticFiles):
    def _allowed(self, path: str) -> bool:
        parts = PurePosixPath(path.replace("\\", "/")).parts
        if len(parts) < 2 or parts[0] not in PUBLIC_FOLDERS:
            return False
        if any(p.startswith(".") for p in parts):  # hidden / temp files, markers
            return False
        if parts[0] == "videos" and (self.directory / parts[0] / parts[1] / INCOMPLETE).exists():  # type: ignore[operator]
            return False
        return True

    async def get_response(self, path: str, scope: Scope) -> Response:
        if not self._allowed(path):
            return PlainTextResponse("Not Found", status_code=404)
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            max_age = PLAYLIST_MAX_AGE if path.endswith(".m3u8") else FILE_MAX_AGE
            response.headers["Cache-Control"] = f"public, max-age={max_age}"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.media_root:
        raise RuntimeError("Set MEDIA_ROOT in .env (e.g. D:\\reelm-media)")
    root = settings.media_root.resolve()
    for folder in PUBLIC_FOLDERS:
        (root / folder).mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Reelm media", docs_url=None, redoc_url=None, openapi_url=None)
    # Public, read-only files: any site may load them (hls.js fetches with CORS).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD"],
        allow_headers=["Range"],
        expose_headers=["Content-Length", "Content-Range", "Accept-Ranges"],
        max_age=86400,
    )
    app.mount("/", MediaFiles(directory=root, html=False), name="media")
    return app


def __getattr__(name: str) -> FastAPI:
    # `app` is built on first use so importing this module (e.g. in tests) needs no .env
    if name == "app":
        return create_app()
    raise AttributeError(name)
