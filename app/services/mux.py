"""Small Mux Video API client + webhook signature check.

Only what Reelm needs: direct uploads, reading uploads/assets, deleting assets.
Docs: https://docs.mux.com/api-reference
"""

import hashlib
import hmac
import time
from typing import Any

import httpx

from app.config import Settings

API_BASE = "https://api.mux.com"


class MuxError(RuntimeError):
    pass


class MuxClient:
    def __init__(
        self,
        token_id: str,
        token_secret: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        self._http = httpx.Client(
            base_url=API_BASE,
            auth=(token_id, token_secret),
            timeout=httpx.Timeout(15.0),
            transport=transport,
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "MuxClient":
        if not settings.mux_enabled:
            raise MuxError(
                "Mux isn't configured: set MUX_TOKEN_ID and MUX_TOKEN_SECRET in .env"
            )
        return cls(settings.mux_token_id, settings.mux_token_secret.get_secret_value())

    def _call(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise MuxError(f"Couldn't reach Mux: {e}") from e
        if r.status_code == 204:
            return {}
        if r.status_code >= 400:
            try:
                messages = r.json().get("error", {}).get("messages") or [r.text]
            except ValueError:
                messages = [r.text]
            raise MuxError(f"Mux {r.status_code}: {'; '.join(map(str, messages))}")
        return r.json().get("data", {})

    def create_upload(self, cors_origin: str) -> dict[str, Any]:
        """A one-time URL the browser can upload a file to directly."""
        return self._call(
            "POST",
            "/video/v1/uploads",
            json={
                "cors_origin": cors_origin,
                "new_asset_settings": {
                    "playback_policies": ["public"],
                    "video_quality": "basic",
                },
            },
        )

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        return self._call("GET", f"/video/v1/uploads/{upload_id}")

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        return self._call("GET", f"/video/v1/assets/{asset_id}")

    def delete_asset(self, asset_id: str) -> None:
        try:
            self._call("DELETE", f"/video/v1/assets/{asset_id}")
        except MuxError as e:
            if "404" not in str(e):  # already gone is fine
                raise

    def close(self) -> None:
        self._http.close()


def verify_webhook(
    body: bytes, signature_header: str | None, secret: str, *, tolerance_sec: int = 300
) -> bool:
    """Check a `Mux-Signature: t=<unix>,v1=<hex hmac>` header.

    The HMAC-SHA256 is over "<t>.<raw body>" with the webhook signing secret.
    Old timestamps are rejected so captured requests can't be replayed later.
    """
    if not signature_header:
        return False
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts.get("t", [""])[0])
    except ValueError:
        return False
    if abs(time.time() - timestamp) > tolerance_sec:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))
