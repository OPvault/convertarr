"""HTTP API for remote `convertarr-worker` processes.

All endpoints under `/api/v1/nodes/...`. Same X-Api-Key auth as the rest of
the app via `require_auth`. The local node never goes through these — it
calls into `workers/execution.py` directly.

Pull-based dispatch: workers ask for jobs (`/claim`), heartbeat with their
in-flight ids (`/heartbeat`), and report progress + finish. Cancellations
ride on the heartbeat response to avoid needing the host to reach
NAT-bound workers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from ..db import session_scope
from ..models import Job, JobState, Node
from ..workers import execution
from .auth import require_auth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/nodes", dependencies=[Depends(require_auth)])


def _node_config_payload(node_id: str) -> dict:
    """Configuration the host pushes back to the worker. `max_concurrent_jobs`
    is NOT pushed — it's owned by the worker (set on the worker's own machine)
    and reported up via register/heartbeat. We only push back the encoder
    choice (still host-controlled). Workers apply changes on the next tick."""
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            return {"encoder_choice": "auto"}
        return {"encoder_choice": n.encoder_choice}


@router.post("/register")
async def register_node(payload: dict, request: Request) -> dict:
    """Worker handshake. Body shape:
        {
            "node_id":       "<uuid>",                   required
            "name":          "desktop",                  optional
            "encoder_family": "vaapi" | "nvenc" | ...,   optional (auto-detected)
            "encoder_name":  "hevc_vaapi",               optional
            "max_concurrent_jobs": 4,                    optional, hint
            "version":       "0.1.0"                     optional
        }
    Upserts the Node row. Subsequent registers from the same node_id refresh
    the encoder fields (in case ffmpeg or hardware changed) and the worker-
    advertised `max_concurrent_jobs` (worker is the source of truth for that
    setting; the operator edits it on the worker's own /settings/nodes).
    Host-owned fields (encoder_choice, path mappings) and the display name
    are left alone after first registration.
    """
    node_id = (payload.get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(400, "node_id is required")
    if node_id == "local":
        # Reserved for the host's built-in worker. Refuse to let a remote
        # claim that name and confuse the watchdog (which exempts is_local).
        raise HTTPException(400, "'local' is reserved for the host's own worker")

    name = (payload.get("name") or node_id[:8]).strip() or node_id[:8]
    encoder_family = payload.get("encoder_family")
    encoder_name = payload.get("encoder_name")
    advertised_max = payload.get("max_concurrent_jobs")
    try:
        worker_max = int(advertised_max) if advertised_max is not None else None
    except (TypeError, ValueError):
        worker_max = None
    if worker_max is not None:
        worker_max = max(0, min(16, worker_max))
    version = payload.get("version")
    address = request.client.host if request.client else None
    now = datetime.now(timezone.utc)

    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            n = Node(
                id=node_id,
                name=name,
                is_local=False,
                encoder_family=encoder_family,
                encoder_name=encoder_name,
                encoder_choice="auto",
                max_concurrent_jobs=worker_max if worker_max is not None else 1,
                last_register=now,
                last_heartbeat=now,
                address=address,
                version=version,
            )
            s.add(n)
            log.info("registered new node %s (%s) family=%s name=%s",
                     node_id, address, encoder_family, name)
        else:
            n.encoder_family = encoder_family
            n.encoder_name = encoder_name
            n.last_register = now
            n.last_heartbeat = now
            n.address = address
            n.version = version
            # Worker is the source of truth for max_concurrent_jobs — pull
            # the latest reported value on every re-register.
            if worker_max is not None:
                n.max_concurrent_jobs = worker_max
            log.info("re-registered node %s (%s)", node_id, address)
        # Snapshot the config to return.
        config = {
            "encoder_choice": n.encoder_choice,
            "name": n.name,
        }

    return {"ok": True, "node_id": node_id, **config}


@router.post("/{node_id}/heartbeat")
async def heartbeat(node_id: str, payload: dict) -> dict:
    """Worker keep-alive + bidirectional config/cancel sync.

    Body:  {
        "running_job_ids": [int, ...],
        "max_concurrent_jobs": int  # worker-controlled; mirrored to Node row
    }
    Reply: {
        "cancellations": [job_id, ...],     # SIGTERM these locally
        "encoder_choice": str
    }
    """
    if node_id == "local":
        raise HTTPException(400, "local node uses in-process keep-alive")

    running = payload.get("running_job_ids") or []
    if not isinstance(running, list):
        raise HTTPException(400, "running_job_ids must be a list")
    try:
        running_ids = [int(x) for x in running]
    except (TypeError, ValueError):
        raise HTTPException(400, "running_job_ids must be ints")

    advertised_max = payload.get("max_concurrent_jobs")
    try:
        worker_max = int(advertised_max) if advertised_max is not None else None
    except (TypeError, ValueError):
        worker_max = None
    if worker_max is not None:
        worker_max = max(0, min(16, worker_max))
    advertised_version = payload.get("version")
    worker_version = str(advertised_version).strip() if advertised_version else None

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            # Worker should re-register before retrying.
            raise HTTPException(404, "unknown node — re-register")
        n.last_heartbeat = now
        # Mirror the worker's locally-configured concurrency cap. Worker is
        # the source of truth — the host's UI shows this read-only.
        if worker_max is not None and n.max_concurrent_jobs != worker_max:
            n.max_concurrent_jobs = worker_max
        if worker_version and n.version != worker_version:
            n.version = worker_version

    cancellations = execution.cancellations_for(node_id, running_ids)
    config = _node_config_payload(node_id)
    return {"ok": True, "cancellations": cancellations, **config}


@router.post("/{node_id}/claim")
async def claim_job(node_id: str) -> dict:
    """Pull next queued job. Atomic — same race-safe SELECT-then-UPDATE used
    by the local worker. Path translation is applied here so the worker
    receives paths it can directly open."""
    if node_id == "local":
        raise HTTPException(400, "local node claims via in-process call")

    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "unknown node — re-register")

    dispatch = execution.claim_for_node(node_id)
    if dispatch is None:
        return {"job": None}
    dispatch = execution.apply_path_mappings(node_id, dispatch)
    return {"job": dispatch.to_dict()}


@router.post("/{node_id}/jobs/{job_id}/start")
async def report_start(node_id: str, job_id: int, payload: dict) -> dict:
    """Worker reports the encoder + final argv it built for a freshly-claimed
    job. Lets the host's dashboard show what's actually being used right when
    the encode starts (rather than waiting until /finish).
    """
    encoder_name = (payload.get("encoder_name") or "").strip()
    raw_args = payload.get("ffmpeg_args") or []
    if not isinstance(raw_args, list):
        raw_args = []
    ffmpeg_args = [str(a) for a in raw_args]
    source_path = (payload.get("source_path") or "").strip() or None
    output_path = (payload.get("output_path") or "").strip() or None
    execution.record_job_start(
        job_id, encoder_name, ffmpeg_args,
        source_path=source_path, output_path=output_path,
    )
    return {"ok": True}


@router.post("/{node_id}/jobs/{job_id}/progress")
async def report_progress(node_id: str, job_id: int, payload: dict) -> dict:
    """Stream progress sample. Returns `cancel: true` as a fast-path cancel
    signal so workers actively reporting progress can react in <1s instead
    of waiting for the next heartbeat (15s)."""
    pct = payload.get("progress_pct")
    speed = payload.get("progress_speed")
    fps = payload.get("progress_fps")
    try:
        pct_f = float(pct) if pct is not None else 0.0
    except (TypeError, ValueError):
        pct_f = 0.0
    speed_f = None
    fps_f = None
    if speed is not None:
        try:
            speed_f = float(speed)
        except (TypeError, ValueError):
            pass
    if fps is not None:
        try:
            fps_f = float(fps)
        except (TypeError, ValueError):
            pass

    # update_progress returns False when the job is no longer in `running`
    # state — that includes `cancelling`, which is the cancel-pending case.
    # We disambiguate cancel from "already terminal" by checking the row.
    still_running = execution.update_progress(job_id, pct_f, speed_f, fps_f)
    cancel = False
    if not still_running:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j is not None and j.state == JobState.cancelling and j.node_id == node_id:
                cancel = True
    return {"ok": True, "cancel": cancel}


@router.post("/{node_id}/jobs/{job_id}/finish")
async def finish_job(node_id: str, job_id: int, payload: dict) -> dict:
    """Worker reports a terminal result. Body matches `JobResult.to_dict()`.
    Host applies the result to the DB, refreshes MediaFile.probe_json on a
    successful encode, and triggers Sonarr/Radarr rescan async.
    """
    try:
        result = execution.JobResult.from_dict(payload)
    except TypeError as e:
        raise HTTPException(400, f"malformed JobResult: {e}")
    await execution.record_finish(node_id, job_id, result)
    return {"ok": True}
