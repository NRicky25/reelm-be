"""Read-only file server for the self-hosted media folder (MEDIA_ROOT).

Serves <MEDIA_ROOT>/images and <MEDIA_ROOT>/videos to the internet through the
Cloudflare Tunnel (media.<domain> -> 127.0.0.1:8090).

Locked down on purpose, since it faces the internet from a home PC:
- GET/HEAD only, no folder listings;
- only files inside images/ or videos/ with a media extension;
- only the exact on-disk spelling of a path: no other letter case, Windows
  short names (PROGRA~1), trailing dots/spaces, NTFS streams (a.jpg:stream),
  links/junctions, hidden (dot) files;
- videos still being encoded (an .incomplete marker in their folder or any
  parent folder below videos/) are hidden.

    uv run uvicorn app.media_server:app --host 127.0.0.1 --port 8090
"""

import mimetypes
import os
import re
from pathlib import Path

import anyio
from fastapi import FastAPI
from starlette.exceptions import HTTPException
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

ALLOWED_EXTENSIONS = {
    "images": {".jpg", ".jpeg", ".png", ".webp"},
    "videos": {".m3u8", ".ts", ".m4s", ".mp4", ".vtt"},
}
INCOMPLETE = ".incomplete"  # same marker reelm-encoder puts in a folder while encoding
# One path part: starts and ends with a letter/digit/_()-, no ':' '~' '\' etc.
NAME = re.compile(r"^[A-Za-z0-9_()-](?:[A-Za-z0-9._ ()-]*[A-Za-z0-9_()-])?$")

PLAYLIST_MAX_AGE = 60
REUSED_NAME_MAX_AGE = 3600  # images/stills/* keep fixed names when re-copied
FILE_MAX_AGE = 7 * 24 * 3600


def _not_found() -> Response:
    return PlainTextResponse(
        "Not Found", status_code=404, headers={"Cache-Control": "no-store"}
    )


class MediaFiles(StaticFiles):
    def __init__(self, root: Path) -> None:
        super().__init__(directory=root, html=False)
        self.root = root

    def _allowed(self, path: str) -> bool:
        """Is `path` (relative, as Starlette gives it) a file we may serve?"""
        parts = path.replace("\\", "/").split("/")
        if len(parts) < 2 or parts[0] not in ALLOWED_EXTENSIONS:
            return False
        if not all(NAME.match(p) for p in parts):
            return False
        if os.path.splitext(parts[-1])[1].lower() not in ALLOWED_EXTENSIONS[parts[0]]:
            return False
        wanted = self.root.joinpath(*parts)
        # realpath follows links/junctions and returns the real spelling, so any
        # alias (case, 8.3 name, junction into another folder) won't match.
        if os.path.realpath(wanted) != str(wanted) or not wanted.is_file():
            return False
        if parts[0] == "videos":
            folder = wanted.parent
            top = self.root / "videos"
            while folder != top:
                if (folder / INCOMPLETE).exists():
                    return False
                folder = folder.parent
        return True

    async def get_response(self, path: str, scope: Scope) -> Response:
        if not await anyio.to_thread.run_sync(self._allowed, path):
            return _not_found()
        try:
            response = await super().get_response(path, scope)
        except HTTPException:
            return _not_found()
        if response.status_code in (200, 206, 304):
            response.headers["Cache-Control"] = cache_control(path)
        return response


def cache_control(path: str) -> str:
    path = path.replace("\\", "/")
    if path.endswith(".m3u8"):
        return f"public, max-age={PLAYLIST_MAX_AGE}"
    if path.startswith("images/stills/"):
        return f"public, max-age={REUSED_NAME_MAX_AGE}"
    # uploads get random names; encoded videos get unique folders
    return f"public, max-age={FILE_MAX_AGE}, immutable"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if not settings.media_root:
        raise RuntimeError("Set MEDIA_ROOT in .env (e.g. D:\\reelm-media)")
    root = Path(os.path.realpath(settings.media_root))
    for folder in ALLOWED_EXTENSIONS:
        (root / folder).mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="Reelm media", docs_url=None, redoc_url=None, openapi_url=None)
    # Public, read-only files: any site may load them (hls.js fetches with CORS).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "HEAD"],
        allow_headers=["Range"],
        expose_headers=["Content-Length", "Content-Range", "Accept-Ranges", "ETag"],
        max_age=86400,
    )
    app.mount("/", MediaFiles(root), name="media")
    return app


def __getattr__(name: str) -> FastAPI:
    # `app` is built on first use so importing this module (e.g. in tests) needs no .env
    if name == "app":
        return create_app()
    raise AttributeError(name)
