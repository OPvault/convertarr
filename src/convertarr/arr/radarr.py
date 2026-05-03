from __future__ import annotations

from .client import ArrClient


class RadarrClient(ArrClient):
    async def list_movies(self) -> list[dict]:
        return await self.get("/movie")  # type: ignore[return-value]

    async def get_movie(self, movie_id: int) -> dict:
        return await self.get(f"/movie/{movie_id}")  # type: ignore[return-value]

    async def rescan_movie(self, movie_id: int) -> dict:
        return await self.post("/command", {"name": "RescanMovie", "movieId": movie_id})  # type: ignore[return-value]

    async def refresh_movie(self, movie_id: int) -> dict:
        return await self.post("/command", {"name": "RefreshMovie", "movieIds": [movie_id]})  # type: ignore[return-value]
