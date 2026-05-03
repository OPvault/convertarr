"""Startup cleanup for orphaned `.CONVERTARR.` output files.

A successful conversion ends with `_finalize_swap` renaming
`name.CONVERTARR.mkv` over `name.mkv`. If ffmpeg crashed, was killed, or the
process exited mid-encode, the partial output file is left on disk consuming
space and potentially confusing the next run.

We only ever look at paths we know about (rows in the MediaFile table). Walking
the user's whole media tree at startup would be too slow and risky.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from ..db import session_scope
from ..encode.plan import output_path_for
from ..models import MediaFile

log = logging.getLogger(__name__)


def cleanup_orphaned_outputs() -> int:
    """Delete every `.CONVERTARR.` file whose source we know about. Returns
    the number of files actually removed. Safe to call before the worker
    starts — at startup no jobs are running yet, so any leftover `.CONVERTARR.`
    file is necessarily from a prior failed/interrupted conversion."""
    removed = 0
    with session_scope() as s:
        paths = s.scalars(select(MediaFile.path)).all()
    for src in paths:
        if not src:
            continue
        try:
            out = output_path_for(src)
            if out.exists():
                size = out.stat().st_size
                out.unlink()
                removed += 1
                log.warning(
                    "removed orphaned converter output: %s (%.2f MB)",
                    out, size / 1_048_576,
                )
        except OSError as e:
            log.warning("failed to clean up %s: %s", src, e)
    return removed
