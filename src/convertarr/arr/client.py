from __future__ import annotations

import httpx


class ArrClient:
    """Minimal v3 API client shared by Sonarr and Radarr."""

    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v3",
            headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
            timeout=self.timeout,
        )

    async def system_status(self) -> dict:
        async with self._client() as c:
            r = await c.get("/system/status")
            r.raise_for_status()
            return r.json()

    async def list_root_folders(self) -> list[dict]:
        """Returns the configured root folders. Same endpoint on Sonarr & Radarr v3."""
        async with self._client() as c:
            r = await c.get("/rootfolder")
            r.raise_for_status()
            return r.json()

    async def get(self, path: str, **params: object) -> object:
        async with self._client() as c:
            r = await c.get(path, params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
            return r.json()

    async def post(self, path: str, json: dict) -> object:
        async with self._client() as c:
            r = await c.post(path, json=json)
            r.raise_for_status()
            return r.json() if r.content else {}
