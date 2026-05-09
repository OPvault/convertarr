"""Host's built-in worker. Runs in the same process as the FastAPI app so
single-machine deployments keep working without any extra setup. Identical
claim/run/finish logic as a remote worker — just bypasses HTTP and calls
the shared execution module directly.

The local node always has `Node.id == LOCAL_NODE_ID` and `is_local=True`.
There's exactly one of these per host process.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..db import session_scope
from ..encode.hwdetect import detect_best
from ..encode.runner import runner
from ..models import Node
from . import execution

log = logging.getLogger(__name__)


# Stable string ID for the host's built-in worker. Anything else is a remote
# worker with a self-generated UUID.
LOCAL_NODE_ID = "local"


def ensure_local_node() -> Node:
    """Upsert the local Node row. Run once on startup. Returns a fresh
    snapshot of the row so the caller can read its current
    max_concurrent_jobs / encoder_choice without re-opening a session.
    """
    from ..web import runtime_settings as rs

    from .. import __version__ as _convertarr_version

    encoder = detect_best()
    now = datetime.now(timezone.utc)

    # Migrate the legacy global `max_concurrent_jobs` setting to a per-node
    # value the first time we boot with multi-node code. After this, the per-
    # node value on the Node row is the source of truth for the local worker.
    legacy_max = 1
    try:
        legacy_max = max(1, min(16, int(rs.get("max_concurrent_jobs", 1))))
    except (TypeError, ValueError):
        legacy_max = 1

    with session_scope() as s:
        node = s.get(Node, LOCAL_NODE_ID)
        if node is None:
            node = Node(
                id=LOCAL_NODE_ID,
                name="(host)",
                is_local=True,
                encoder_family=encoder.family,
                encoder_name=encoder.name,
                encoder_choice=rs.get("encoder_choice", "auto"),
                max_concurrent_jobs=legacy_max,
                last_register=now,
                last_heartbeat=now,
                version=_convertarr_version,
            )
            s.add(node)
            s.flush()
            log.info("seeded local Node row (encoder=%s, max=%d)", encoder.name, legacy_max)
        else:
            # Refresh encoder fields each startup — the user might have
            # installed/upgraded ffmpeg or swapped a GPU. Don't touch
            # max_concurrent_jobs (user-editable from the UI).
            node.encoder_family = encoder.family
            node.encoder_name = encoder.name
            node.last_register = now
            node.last_heartbeat = now
            node.is_local = True
            node.version = _convertarr_version
            if not node.name:
                node.name = "(host)"
        # Detach a frozen snapshot for the caller.
        return Node(
            id=node.id, name=node.name, is_local=node.is_local,
            encoder_family=node.encoder_family, encoder_name=node.encoder_name,
            encoder_choice=node.encoder_choice,
            max_concurrent_jobs=node.max_concurrent_jobs,
            last_heartbeat=node.last_heartbeat, last_register=node.last_register,
            address=node.address, version=node.version, created_at=node.created_at,
        )


def _read_local_max_concurrency() -> int:
    """Read the local node's `max_concurrent_jobs` fresh each loop tick so the
    user can change it from the UI without a worker restart. Clamped [1, 16].
    """
    with session_scope() as s:
        node = s.get(Node, LOCAL_NODE_ID)
        n = node.max_concurrent_jobs if node else 1
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(16, n))


async def _run_one_dispatch(dispatch: execution.JobDispatch) -> None:
    """Wrap execute_dispatch + record_finish for a single in-process job."""
    encoder = detect_best()
    try:
        result = await execution.execute_dispatch(dispatch, encoder, runner)
    except Exception as e:
        log.exception("local worker crashed running job %d", dispatch.job_id)
        result = execution.JobResult(
            success=False, was_cancelled=False, returncode=None,
            error_tail=repr(e), encoder_name=encoder.name,
        )
    await execution.record_finish(LOCAL_NODE_ID, dispatch.job_id, result)


async def local_worker_loop(stop: asyncio.Event) -> None:
    """Pool-based local worker. Drop-in replacement for the legacy
    `worker_loop` — same shape (claim → dispatch → reap), just goes through
    the shared `execution` module instead of inline code.

    Heartbeat-keep-alive is a no-op for the local node (the host process IS
    the local worker; if it's alive the node is alive). `last_heartbeat`
    just gets stamped each loop tick so the Nodes UI shows it as online.
    """
    from ..config import settings as static_settings

    log.info("local worker loop started")
    execution.reconcile_stuck_jobs()
    running: dict[int, asyncio.Task] = {}
    stop_task = asyncio.create_task(stop.wait(), name="convertarr-local-stop")
    try:
        while not stop.is_set():
            # Reap completed
            for jid in list(running):
                if running[jid].done():
                    t = running.pop(jid)
                    exc = t.exception()
                    if exc is not None:
                        log.error("local job %d task crashed: %r", jid, exc)

            # Stamp heartbeat so the Nodes UI shows the local node as live.
            with session_scope() as s:
                node = s.get(Node, LOCAL_NODE_ID)
                if node is not None:
                    node.last_heartbeat = datetime.now(timezone.utc)

            max_concurrent = _read_local_max_concurrency()
            while len(running) < max_concurrent:
                dispatch = execution.claim_for_node(LOCAL_NODE_ID)
                if dispatch is None:
                    break
                # Local node — paths are already host-relative and identical
                # to what the local worker sees, so apply_path_mappings is a
                # no-op (no NodePathMapping rows for the local node by default).
                dispatch = execution.apply_path_mappings(LOCAL_NODE_ID, dispatch)
                running[dispatch.job_id] = asyncio.create_task(
                    _run_one_dispatch(dispatch),
                    name=f"convertarr-local-job-{dispatch.job_id}",
                )
                log.info(
                    "local dispatched job %d (%d/%d slots)",
                    dispatch.job_id, len(running), max_concurrent,
                )

            waiters = [stop_task, *running.values()]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=static_settings.worker_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                log.exception("local_worker_loop wait failed")
    finally:
        stop_task.cancel()
        if running:
            log.info("draining %d in-flight local job(s) on shutdown", len(running))
            await asyncio.gather(*running.values(), return_exceptions=True)
        log.info("local worker loop stopped")
