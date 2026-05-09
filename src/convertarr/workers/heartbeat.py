"""Heartbeat watchdog. Runs in the host process alongside the indexer +
local worker.

A node that hasn't checked in within `STALE_THRESHOLD_SECONDS` is treated as
gone: any Job rows still attributed to it that are in `running` /
`cancelling` state get reset to `queued` (clearing the node attribution and
the started_at timestamp) so a healthy node can claim them on its next
poll. The local node is exempt — if the host process is dead the whole app
is down anyway, so its heartbeat staleness doesn't reflect a worker failure.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from ..db import session_scope
from ..models import Job, JobState, Node

log = logging.getLogger(__name__)


# Conservative bounds — a network blip shouldn't trigger a job revival, but a
# truly-dead worker shouldn't strand jobs for too long either. With a 15s
# heartbeat interval on the worker side, 90s of silence is ~6 missed beats.
STALE_THRESHOLD_SECONDS = 90
WATCHDOG_TICK_SECONDS = 30


def _revive_jobs_for_node(node_id: str) -> int:
    """Reset every running/cancelling job belonging to `node_id` back to
    queued. Returns the number of jobs revived. Logs each revival individually
    so the user sees in the logs which encodes are getting redelivered.
    """
    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(
                Job.node_id == node_id,
                Job.state.in_([JobState.running, JobState.cancelling]),
            )
        ).all()
        revived = 0
        for j in rows:
            log.warning(
                "watchdog: reviving job %d (was %s on offline node %s)",
                j.id, j.state.value, node_id,
            )
            j.state = JobState.queued
            j.node_id = None
            j.started_at = None
            j.progress_pct = 0.0
            j.progress_speed = None
            j.progress_fps = None
            revived += 1
        return revived


def sweep_once() -> dict[str, int]:
    """One pass over all non-local nodes. Returns a small summary dict so the
    caller (or tests) can see what happened.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_THRESHOLD_SECONDS)
    offline_nodes: list[str] = []
    revived_total = 0

    with session_scope() as s:
        nodes = s.scalars(select(Node).where(Node.is_local.is_(False))).all()
        for n in nodes:
            hb = n.last_heartbeat
            if hb and hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if hb is not None and hb > cutoff:
                continue
            offline_nodes.append(n.id)

    for node_id in offline_nodes:
        revived_total += _revive_jobs_for_node(node_id)

    return {"checked": len(offline_nodes), "revived": revived_total}


async def watchdog_loop(stop: asyncio.Event) -> None:
    log.info("heartbeat watchdog started (tick=%ds, threshold=%ds)",
             WATCHDOG_TICK_SECONDS, STALE_THRESHOLD_SECONDS)
    try:
        while not stop.is_set():
            try:
                summary = sweep_once()
                if summary["revived"]:
                    log.warning(
                        "watchdog: revived %d job(s) from %d offline node(s)",
                        summary["revived"], summary["checked"],
                    )
            except Exception:
                log.exception("watchdog sweep failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=WATCHDOG_TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("heartbeat watchdog stopped")
