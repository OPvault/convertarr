"""Worker supervisor. Picks between two operating modes based on whether
this Convertarr instance is currently paired with another host:

  - **host mode** (default): runs `local_worker_loop`, which claims jobs
    from this instance's own queue and runs them locally.
  - **worker mode** (paired): runs the `WorkerLoop` from
    `convertarr.worker.loop`, which registers with the paired host, pulls
    jobs from its queue, runs them locally, and reports back over HTTP.

The pairing settings live in `runtime_settings` (`paired_host_url`,
`paired_host_api_key`, etc.), set/cleared by the pairing API endpoints.
The supervisor polls them every few seconds and switches modes when the
target changes — running jobs drain naturally before the new mode starts.
"""
from __future__ import annotations

import asyncio
import logging

from ..web import runtime_settings as rs

log = logging.getLogger(__name__)


SUPERVISOR_TICK_SECONDS = 5


def _current_target() -> str:
    """`"remote"` if a pairing is configured, otherwise `"local"`."""
    return "remote" if rs.get("paired_host_url", None) else "local"


async def _run_local(stop: asyncio.Event) -> None:
    """Run the host-mode loop. Imported lazily so worker mode doesn't pay
    the import cost when the loop isn't going to be used."""
    from .local_node import local_worker_loop
    await local_worker_loop(stop)


async def _run_remote(stop: asyncio.Event) -> None:
    """Spin up an in-process WorkerLoop that connects back to the paired
    host. Reads pairing settings fresh — handles the case where the user
    re-pairs while we're still starting up."""
    from ..worker.client import WorkerClient
    from ..worker.loop import WorkerLoop

    host_url = rs.get("paired_host_url", "")
    api_key = rs.get("paired_host_api_key", "")
    node_id = rs.get("paired_node_id", "")
    name = rs.get("paired_node_name", "") or "worker"
    if not host_url or not api_key or not node_id:
        log.error(
            "supervisor: remote mode requested but pairing config is incomplete; "
            "falling back to local mode"
        )
        await _run_local(stop)
        return

    # `max_concurrent_jobs` is owned by THIS worker's local Node row — the
    # operator edits it on this machine's own /settings/nodes page. The
    # worker reads it fresh each tick and reports it to the host via
    # register/heartbeat. The host treats the worker as the source of truth.
    client = WorkerClient(host_url, api_key)
    loop = WorkerLoop(
        client=client,
        node_id=node_id,
        node_name=name,
    )
    log.warning(
        "supervisor: entering remote-worker mode (host=%s node_id=%s name=%s)",
        host_url, node_id, name,
    )
    await loop.run_forever(stop)


async def supervisor_loop(stop: asyncio.Event) -> None:
    """Outer loop. Watches `paired_host_url` and runs the right inner loop.
    A mode switch sets a new inner stop event so the running loop drains
    cleanly (its in-flight jobs finish first); then the new loop starts.
    """
    inner_task: asyncio.Task | None = None
    inner_stop: asyncio.Event | None = None
    current_mode: str | None = None

    log.info("worker supervisor started")
    try:
        while not stop.is_set():
            target = _current_target()

            if target != current_mode:
                # Drain the old loop before swapping.
                if inner_task is not None and inner_stop is not None:
                    log.warning(
                        "supervisor: switching mode %s -> %s; draining current loop",
                        current_mode, target,
                    )
                    inner_stop.set()
                    try:
                        await asyncio.wait_for(inner_task, timeout=120)
                    except asyncio.TimeoutError:
                        log.warning("supervisor: drain timed out, cancelling loop")
                        inner_task.cancel()
                        try:
                            await inner_task
                        except (asyncio.CancelledError, Exception):
                            pass

                inner_stop = asyncio.Event()
                if target == "local":
                    inner_task = asyncio.create_task(
                        _run_local(inner_stop), name="convertarr-mode-local",
                    )
                else:
                    inner_task = asyncio.create_task(
                        _run_remote(inner_stop), name="convertarr-mode-remote",
                    )
                current_mode = target
                log.info("supervisor: now in %s mode", current_mode)

            # Tick — wake on outer stop or after the supervisor interval.
            try:
                await asyncio.wait_for(stop.wait(), timeout=SUPERVISOR_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        if inner_task is not None and inner_stop is not None:
            inner_stop.set()
            try:
                await asyncio.wait_for(inner_task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                inner_task.cancel()
        log.info("worker supervisor stopped")
