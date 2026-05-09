"""HTTP client wrapping the host's `/api/v1/nodes/...` endpoints.

Plain async httpx + a fixed X-Api-Key header. No retries here — the loop in
`worker/loop.py` handles transient failures by simply trying again on the
next tick. Adding aggressive retries inside the client would mask outages
the operator should know about.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class WorkerClient:
    def __init__(self, host_url: str, api_key: str, timeout_seconds: float = 60.0) -> None:
        # Strip a trailing slash so we can naively concatenate path suffixes.
        self.base = host_url.rstrip("/")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            headers={
                "X-Api-Key": api_key,
                # Accept: application/json forces the host's auth to treat
                # us as a non-browser client. Without this it sees us as a
                # browser navigation and 303-redirects to /login on auth
                # failure (instead of returning a clean 401), which makes
                # debugging key-mismatch problems painful.
                "Accept": "application/json",
            },
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, json: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        r = await self._http.post(url, json=json or {})
        # Friendly errors — surface 401/303/404 with messages a user can act on.
        if r.status_code == 401:
            raise RuntimeError(
                "host rejected the API key (401). The pairing was set up "
                "with a key that no longer matches the host's current api_key "
                "(check the host's Settings → General). Unpair + repair to fix."
            )
        if r.status_code == 303:
            # Falls through `_is_browser_request` on the host side. With the
            # `Accept: application/json` we now send this should never happen;
            # left in as a defensive message in case the host is on an older
            # build that still returns 303.
            loc = r.headers.get("location", "")
            if "/login" in loc:
                raise RuntimeError(
                    "host bounced us to /login (303). API key is wrong or the "
                    "host's auth is misconfigured. Unpair + repair to fix."
                )
        if r.status_code == 404 and path != "/api/v1/nodes/register":
            # The most common cause: heartbeat/claim before the host has a row
            # for this node (host DB wiped, or worker config from a stale run).
            raise RuntimeError(f"host returned 404 for {path}. Re-register required.")
        r.raise_for_status()
        return r.json()

    async def register(self, payload: dict) -> dict:
        return await self._post("/api/v1/nodes/register", payload)

    async def heartbeat(
        self,
        node_id: str,
        running_job_ids: list[int],
        max_concurrent_jobs: int,
        version: str | None = None,
    ) -> dict:
        body: dict = {
            "running_job_ids": list(running_job_ids),
            "max_concurrent_jobs": int(max_concurrent_jobs),
        }
        if version is not None:
            body["version"] = version
        return await self._post(
            f"/api/v1/nodes/{node_id}/heartbeat", body,
        )

    async def claim(self, node_id: str) -> dict:
        return await self._post(f"/api/v1/nodes/{node_id}/claim")

    async def start(self, node_id: str, job_id: int, payload: dict) -> dict:
        return await self._post(
            f"/api/v1/nodes/{node_id}/jobs/{job_id}/start", payload,
        )

    async def progress(self, node_id: str, job_id: int, payload: dict) -> dict:
        return await self._post(
            f"/api/v1/nodes/{node_id}/jobs/{job_id}/progress", payload,
        )

    async def finish(self, node_id: str, job_id: int, payload: dict) -> dict:
        return await self._post(
            f"/api/v1/nodes/{node_id}/jobs/{job_id}/finish", payload,
        )
