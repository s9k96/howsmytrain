"""
Minimal RailRadar API client.

Docs: https://railradar.in/docs
Free tier: 50 requests/day, 10 requests/minute burst.
Auth: Authorization: Bearer <api_key>
"""
import httpx

from . import config


class RailRadarError(Exception):
    """Raised for any non-success response from RailRadar."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class RailRadarRateLimitError(RailRadarError):
    """Raised specifically on HTTP 429 (daily/burst limit hit)."""


class RailRadarClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, timeout: float = 15.0):
        self.api_key = api_key or config.require_api_key()
        self.base_url = (base_url or config.RAILRADAR_BASE_URL).rstrip("/")
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def get_live_status(self, train_number: str) -> dict:
        """
        Returns the `data` payload from GET /trains/{number}/live.

        Raises RailRadarRateLimitError on 429, RailRadarError on any other
        non-2xx response or when the envelope's `success` field is false.
        """
        url = f"{self.base_url}/trains/{train_number}/live"
        try:
            resp = httpx.get(url, headers=self._headers(), timeout=self.timeout)
        except httpx.RequestError as exc:
            raise RailRadarError(f"Network error calling RailRadar: {exc}") from exc

        if resp.status_code == 429:
            raise RailRadarRateLimitError(
                "RailRadar rate limit hit (free tier: 50/day, 10/min).", status_code=429
            )
        if resp.status_code >= 400:
            raise RailRadarError(
                f"RailRadar returned HTTP {resp.status_code}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            envelope = resp.json()
        except ValueError as exc:
            raise RailRadarError(f"RailRadar returned non-JSON response: {resp.text[:300]}") from exc

        if not envelope.get("success", False):
            raise RailRadarError(f"RailRadar reported failure: {envelope}")

        return envelope.get("data", {})
