"""Image storage in the local media folder (served at MEDIA_PUBLIC_URL).

Images are written to <MEDIA_ROOT>/images/<folder>/<name>.<ext> and shown from
<MEDIA_PUBLIC_URL>/images/<folder>/<name>.<ext>. The media server
(app/media_server.py, behind the Cloudflare Tunnel) serves that folder read-only,
so only this backend (and the encoder, for videos) can add or remove files.
"""

import os
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

from app.config import Settings

# What the admin may upload, and how big (posters/backdrops/thumbnails).
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_BYTES = 8 * 1024 * 1024
IMAGES_DIR = "images"


def public_prefix(media_public_url: str) -> str:
    """Start of every public image URL."""
    return f"{media_public_url.rstrip('/')}/{IMAGES_DIR}/"


class StorageError(RuntimeError):
    pass


def sniff_image_type(data: bytes) -> str | None:
    """Detect the real image type from its first bytes (don't trust the filename)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class MediaStorage:
    def __init__(self, root: Path | str, public_url: str) -> None:
        self.dir = Path(root).resolve() / IMAGES_DIR
        self.prefix = public_prefix(public_url)

    @classmethod
    def from_settings(cls, settings: Settings) -> "MediaStorage":
        if not settings.storage_enabled:
            raise StorageError(
                "Image storage isn't configured: set MEDIA_ROOT and MEDIA_PUBLIC_URL"
            )
        assert settings.media_root and settings.media_public_url
        return cls(settings.media_root, settings.media_public_url)

    def _file(self, path: str) -> Path:
        """The file for a relative image path; refuses anything outside the folder."""
        target = (self.dir / path).resolve()
        if not target.is_relative_to(self.dir) or target == self.dir:
            raise StorageError(f"Bad image path: {path!r}")
        return target

    def public_url(self, path: str) -> str:
        return self.prefix + quote(path)

    def path_from_url(self, url: str | None) -> str | None:
        """The image path, if this URL points into our images folder (else None)."""
        if not url or not url.startswith(self.prefix):
            return None
        return unquote(url[len(self.prefix) :]) or None

    def save(self, path: str, data: bytes) -> str:
        """Write bytes at a relative path (atomically) and return the public URL."""
        target = self._file(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            tmp.write_bytes(data)
            os.replace(tmp, target)
        except OSError as e:
            raise StorageError(f"Couldn't save the image: {e}") from e
        return self.public_url(path)

    def upload(self, folder: str, data: bytes, *, name: str | None = None) -> str:
        """Store an image and return its public URL. Validates type and size."""
        if len(data) > MAX_BYTES:
            raise StorageError(
                f"Image is too large (max {MAX_BYTES // (1024 * 1024)} MB)."
            )
        content_type = sniff_image_type(data)
        if content_type is None:
            raise StorageError("Only JPEG, PNG or WebP images can be uploaded.")
        path = f"{folder.strip('/')}/{name or uuid.uuid4().hex}.{ALLOWED_TYPES[content_type]}"
        return self.save(path, data)

    def delete(self, paths: list[str]) -> None:
        for path in paths:
            try:
                self._file(path).unlink(missing_ok=True)
            except OSError as e:
                raise StorageError(f"Couldn't delete {path}: {e}") from e

    def close(self) -> None:
        """Nothing to close (kept so callers can treat storage like a client)."""
