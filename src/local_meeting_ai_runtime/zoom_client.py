"""Minimal Zoom REST client for the delegate runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from typing import Any
from urllib.parse import quote

import httpx


class ZoomRuntimeError(RuntimeError):
    """Raised when the delegate runtime cannot reach Zoom correctly."""


class ZoomRestClient:
    def __init__(self) -> None:
        self._access_token = os.getenv("ZOOM_ACCESS_TOKEN")
        self._client_id = os.getenv("ZOOM_CLIENT_ID")
        self._client_secret = os.getenv("ZOOM_CLIENT_SECRET")
        self._account_id = os.getenv("ZOOM_ACCOUNT_ID")
        self._api_base_url = os.getenv("ZOOM_API_BASE_URL", "https://api.zoom.us/v2").rstrip("/")
        self._oauth_base_url = os.getenv("ZOOM_OAUTH_BASE_URL", "https://zoom.us").rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=float(os.getenv("ZOOM_TIMEOUT_SECONDS", "30")),
            verify=self._env_bool("ZOOM_VERIFY_SSL", True),
        )
        self._cached_token: str | None = None
        self._token_expiry: datetime | None = None

    def _env_bool(self, name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() not in {"0", "false", "no", "off"}

    async def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        if self._cached_token and self._token_expiry and datetime.now(UTC) < self._token_expiry:
            return self._cached_token
        if not all([self._client_id, self._client_secret, self._account_id]):
            raise ZoomRuntimeError("Zoom credentials are incomplete for the delegate runtime.")

        response = await self._http.post(
            f"{self._oauth_base_url}/oauth/token",
            params={"grant_type": "account_credentials", "account_id": self._account_id},
            auth=(self._client_id, self._client_secret),
        )
        payload = response.json()
        if response.is_error:
            raise ZoomRuntimeError(f"Zoom OAuth token request failed: {payload}")

        token = payload.get("access_token")
        if not token:
            raise ZoomRuntimeError(f"Zoom OAuth response did not contain access_token: {payload}")
        expires_in = int(payload.get("expires_in", 3500))
        self._cached_token = token
        self._token_expiry = datetime.now(UTC) + timedelta(seconds=max(expires_in - 60, 60))
        return token

    def _decode_payload(self, response: httpx.Response) -> Any:
        if response.status_code == 204:
            return None
        if "application/json" in response.headers.get("content-type", "").lower():
            return response.json()
        return response.text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = await self._get_access_token()
        response = await self._http.request(
            method.upper(),
            f"{self._api_base_url}{path}",
            json=body,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        payload = self._decode_payload(response)
        if response.is_error:
            raise ZoomRuntimeError(f"Zoom request failed for {method.upper()} {path}: {payload}")
        return payload

    async def get_meeting(self, meeting_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/meetings/{self._encode_meeting_ref(meeting_id)}")

    def _encode_meeting_ref(self, meeting_ref: str) -> str:
        raw = str(meeting_ref or "").strip()
        if not raw:
            return raw
        if "/" not in raw:
            return raw
        # Zoom meeting UUIDs can contain `/` and require double encoding in path params.
        return quote(quote(raw, safe=""), safe="")
