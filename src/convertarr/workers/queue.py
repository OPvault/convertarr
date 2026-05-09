"""Backwards-compatibility shim. The real worker logic moved to:

  - `convertarr.workers.execution` (shared host/worker job-execution core)
  - `convertarr.workers.local_node` (host's built-in worker loop)
  - `convertarr.workers.heartbeat`  (offline-node watchdog)

Older imports of `worker_loop`, `_total_frames_from_probe`, and
`BACKUP_DIR_NAME` from this module still resolve via the re-exports below
so external scripts and tests don't break.
"""
from __future__ import annotations

from .execution import (
    BACKUP_DIR_NAME,
    JobDispatch,
    JobResult,
    finalize_swap as _finalize_swap,
    reconcile_stuck_jobs as _reconcile_stuck_jobs,
    total_frames_from_probe as _total_frames_from_probe,
)
from .local_node import local_worker_loop as worker_loop

__all__ = [
    "BACKUP_DIR_NAME",
    "JobDispatch",
    "JobResult",
    "_finalize_swap",
    "_reconcile_stuck_jobs",
    "_total_frames_from_probe",
    "worker_loop",
]
