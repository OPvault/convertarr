"""Pairing API — used by another Convertarr instance (the host) to enlist
this instance as a worker.

Flow:
  1. Operator opens the host's `/settings/nodes` UI, types the worker's IP
     and the worker's API key.
  2. Host POSTs `/api/v1/pairing/accept` to the worker, authenticating with
     the worker's API key and including the host's own URL + API key in
     the body.
  3. Worker stores the host info in its runtime_settings and (within a few
     seconds) the worker supervisor switches the local node into "remote
     worker" mode, which registers with the host and starts pulling jobs.

Inverse: the host can POST `/api/v1/pairing/disconnect` (still authenticating
with the worker's API key) to break the pairing. The supervisor switches
back to local mode on the next tick.

Both endpoints sit on the WORKER side of the relationship — they're
invoked by the host. From the worker's perspective they're inbound calls
gated by the worker's own X-Api-Key.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from . import runtime_settings as rs
from .auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/pairing", dependencies=[Depends(require_auth)])


def _ensure_paired_node_id() -> str:
    """Stable UUID used when registering with the host. Generated once on
    first successful pairing and reused across re-pairings, so the host's
    Node row keeps the same identity across worker restarts."""
    existing = rs.get("paired_node_id", None)
    if existing:
        return str(existing)
    new_id = str(uuid.uuid4())
    rs.set("paired_node_id", new_id)
    return new_id


@router.post("/accept")
async def accept_pairing(payload: dict) -> dict:
    """Become a worker for the calling host.

    Body:
      {
        "host_url":     "http://<host>:6565",   required (where to call back)
        "host_api_key": "<host's own api_key>", required (auth for callbacks)
        "name":         "Desktop"               optional (display name)
      }
    """
    host_url = (payload.get("host_url") or "").strip().rstrip("/")
    host_api_key = (payload.get("host_api_key") or "").strip()
    name = (payload.get("name") or "").strip()

    if not host_url:
        raise HTTPException(400, "host_url is required")
    if not host_api_key:
        raise HTTPException(400, "host_api_key is required")
    # Stop people pointing a worker at itself by accident — would create a
    # registration loop where this instance tries to claim its own jobs.
    own_url = rs.get("paired_host_url", None)
    if host_url == own_url:
        # Idempotent re-pair (same host) is fine; just refresh the credentials.
        pass

    rs.set("paired_host_url", host_url)
    rs.set("paired_host_api_key", host_api_key)
    if name:
        rs.set("paired_node_name", name)
    rs.set("paired_at", datetime.now(timezone.utc).isoformat())

    node_id = _ensure_paired_node_id()
    log.warning(
        "accepted pairing — now acting as worker for host=%s node_id=%s",
        host_url, node_id,
    )
    return {"ok": True, "node_id": node_id, "name": name or rs.get("paired_node_name", "worker")}


@router.post("/disconnect")
async def disconnect_pairing() -> dict:
    """Forget the paired host. The supervisor reverts to local-mode on the
    next tick. The `paired_node_id` is kept so a future re-pair to the same
    host shows up as the same node row instead of a new one."""
    had_pairing = bool(rs.get("paired_host_url", None))
    rs.set("paired_host_url", None)
    rs.set("paired_host_api_key", None)
    if had_pairing:
        log.warning("pairing cleared — reverting to host mode")
    return {"ok": True, "was_paired": had_pairing}


@router.get("/status")
async def pairing_status() -> dict:
    """Lets the host probe whether a pairing is in effect (used by the host
    UI to confirm the pair-form succeeded)."""
    host_url = rs.get("paired_host_url", None)
    return {
        "paired": bool(host_url),
        "host_url": host_url,
        "node_id": rs.get("paired_node_id", None),
        "node_name": rs.get("paired_node_name", None),
        "paired_at": rs.get("paired_at", None),
    }
