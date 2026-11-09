"""Image storage on Supabase Storage (a public bucket served by Supabase's CDN).

Images are stored under generated names and served from public URLs, so the
frontend needs no key to show them. Only this backend (with the service-role
key) can upload or delete.
Docs: https://supabase.com/docs/reference/api (Storage API)
"""

import uuid
from typing import Any

import httpx

from app.config import Settings

# What the admin may upload, and how big (posters/backdrops; videos go to Mux).
ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
MAX_BYTES = 8 * 1024 * 1024


class StorageError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def sniff_image_type(data: bytes) -> str | None:
    """Detect the real image type from its first bytes (don't trust the filename)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


class SupabaseStorage:
    def __init__(
        self,
        url: str,
        key: str,
        bucket: str = "images",
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base = url.rstrip("/")
        self.bucket = bucket
        self._http = httpx.Client(
            base_url=f"{self.base}/storage/v1",
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "SupabaseStorage":
        if not settings.storage_enabled:
            raise StorageError(
                "Image storage isn't configured: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY"
            )
        return cls(
            settings.supabase_url,
            settings.supabase_service_role_key.get_secret_value(),
            settings.supabase_bucket,
        )

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise StorageError(f"Couldn't reach Supabase Storage: {e}") from e
        if r.status_code >= 400:
            try:
                detail = r.json().get("message") or r.text
            except ValueError:
                detail = r.text
            raise StorageError(f"Storage {r.status_code}: {detail}", r.status_code)
        return (
            r.json()
            if r.content and "json" in r.headers.get("content-type", "")
            else None
        )

    def ensure_bucket(self) -> None:
        """Create the public image bucket if it doesn't exist yet."""
        try:
            self._call("GET", f"/bucket/{self.bucket}")
            return
        except StorageError as e:
            if e.status_code not in (400, 404):
                raise
        self._call(
            "POST",
            "/bucket",
            json={
                "id": self.bucket,
                "name": self.bucket,
                "public": True,
                "file_size_limit": MAX_BYTES,
                "allowed_mime_types": list(ALLOWED_TYPES),
            },
        )

    def public_url(self, path: str) -> str:
        return f"{self.base}/storage/v1/object/public/{self.bucket}/{path}"

    def path_from_url(self, url: str | None) -> str | None:
        """The object path, if this URL points into our bucket (else None)."""
        prefix = f"{self.base}/storage/v1/object/public/{self.bucket}/"
        return url[len(prefix) :] if url and url.startswith(prefix) else None

    def upload(self, folder: str, data: bytes, *, name: str | None = None) -> str:
        """Upload an image and return its public URL. Validates type and size."""
        if len(data) > MAX_BYTES:
            raise StorageError(
                f"Image is too large (max {MAX_BYTES // (1024 * 1024)} MB)."
            )
        content_type = sniff_image_type(data)
        if content_type is None:
            raise StorageError("Only JPEG, PNG or WebP images can be uploaded.")
        path = f"{folder.strip('/')}/{name or uuid.uuid4().hex}.{ALLOWED_TYPES[content_type]}"
        self._call(
            "POST",
            f"/object/{self.bucket}/{path}",
            content=data,
            headers={
                "Content-Type": content_type,
                "x-upsert": "true",
                "cache-control": "max-age=31536000",  # names are unique, so cache "forever"
            },
        )
        return self.public_url(path)

    def delete(self, paths: list[str]) -> None:
        if paths:
            self._call("DELETE", f"/object/{self.bucket}", json={"prefixes": paths})

    def close(self) -> None:
        self._http.close()
