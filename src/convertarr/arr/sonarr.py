from __future__ import annotations

from .client import ArrClient


class SonarrClient(ArrClient):
    async def list_series(self) -> list[dict]:
        return await self.get("/series")  # type: ignore[return-value]

    async def get_series(self, series_id: int) -> dict:
        return await self.get(f"/series/{series_id}")  # type: ignore[return-value]

    async def episode_files(self, series_id: int) -> list[dict]:
        return await self.get("/episodefile", seriesId=series_id)  # type: ignore[return-value]

    async def episode_file(self, episode_file_id: int) -> dict:
        return await self.get(f"/episodefile/{episode_file_id}")  # type: ignore[return-value]

    async def episodes(self, series_id: int) -> list[dict]:
        return await self.get("/episode", seriesId=series_id)  # type: ignore[return-value]

    async def rescan_series(self, series_id: int) -> dict:
        return await self.post("/command", {"name": "RescanSeries", "seriesId": series_id})  # type: ignore[return-value]

    async def refresh_series(self, series_id: int) -> dict:
        """Refresh series metadata (and Sonarr internally rescans disk too)."""
        return await self.post("/command", {"name": "RefreshSeries", "seriesId": series_id})  # type: ignore[return-value]
