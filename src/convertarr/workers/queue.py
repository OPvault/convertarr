from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update

from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..config import settings
from ..db import session_scope
from ..encode.hwdetect import detect_best
from ..encode.plan import build_ffmpeg_args, output_path_for
from ..encode.runner import runner
from ..models import ArrInstance, ArrKind, Job, JobState, MediaFile
from ..probe.ffprobe import ffprobe
from ..probe.policy import FilePlan, StreamPlan, evaluate

log = logging.getLogger(__name__)


def _file_plan_from_db(mf: MediaFile) -> FilePlan:
    """Re-evaluate policy on the persisted probe JSON. Cheaper than re-probing."""
    probe = mf.probe_json or {}
    return evaluate(probe, settings.policy)


def _total_frames_from_probe(probe: dict | None, duration_seconds: float | None) -> int | None:
    """Best-effort total-frame count for the primary video stream.

    Source order: matroska's NUMBER_OF_FRAMES tag → ffprobe's `nb_frames` →
    `duration * r_frame_rate`. Used as the ground truth for progress %
    because VAAPI's `out_time_us` ships as `N/A` during encoding.
    """
    if not probe:
        return None
    streams = probe.get("streams") or []
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if not video:
        return None
    # Matroska tag (most reliable when present)
    tags = video.get("tags") or {}
    for k in ("NUMBER_OF_FRAMES", "NUMBER_OF_FRAMES-eng"):
        if tags.get(k):
            try:
                return int(tags[k])
            except (TypeError, ValueError):
                pass
    nb = video.get("nb_frames")
    if nb and nb != "N/A":
        try:
            return int(nb)
        except (TypeError, ValueError):
            pass
    # Fallback: duration * r_frame_rate (e.g. "24000/1001")
    rfr = video.get("r_frame_rate") or video.get("avg_frame_rate") or ""
    if duration_seconds and "/" in rfr:
        try:
            num, den = rfr.split("/", 1)
            num, den = int(num), int(den)
            if den:
                return int(round(duration_seconds * num / den))
        except ValueError:
            pass
    return None


async def _notify_arr_refresh_and_rescan(instance: dict | None, entity_id: int | None) -> None:
    """Tell the *arr to refresh metadata + rescan disk so the new file is picked up
    immediately (filesize and codec stats updated in Sonarr/Radarr's own DB)."""
    if instance is None or entity_id is None:
        return
    try:
        if instance["kind"] == ArrKind.sonarr:
            client = SonarrClient(instance["base_url"], instance["api_key"])
            await client.refresh_series(entity_id)
            await client.rescan_series(entity_id)
        elif instance["kind"] == ArrKind.radarr:
            client = RadarrClient(instance["base_url"], instance["api_key"])
            await client.refresh_movie(entity_id)
            await client.rescan_movie(entity_id)
    except Exception as e:
        log.warning("post-conversion arr notification failed: %s", e)


def _finalize_swap(input_path: Path, output_path: Path) -> tuple[bool, str | None]:
    """Replace the original file with the converted one. Default behavior
    backs up the original to `<dir>/.old/<name>`; if the user opts in via the
    `delete_originals` setting the original is permanently deleted instead.

    Returns (ok, error_message). Same-filesystem rename is atomic.
    """
    from ..web import runtime_settings as rs
    delete_originals = bool(rs.get("delete_originals", False))
    try:
        if delete_originals:
            input_path.unlink(missing_ok=True)
            log.warning(
                "DELETED original (delete_originals=true): %s",
                input_path,
            )
        else:
            backup_dir = input_path.parent / ".old"
            backup_dir.mkdir(exist_ok=True)
            backup_target = backup_dir / input_path.name
            if backup_target.exists():
                log.info("backup already exists, skipping move: %s", backup_target)
                input_path.unlink(missing_ok=True)
            else:
                input_path.rename(backup_target)
                log.info("backed up original: %s -> %s", input_path, backup_target)
        output_path.rename(input_path)
        log.info("swapped converted file into place: %s", input_path)
        return True, None
    except OSError as e:
        return False, f"finalize_swap failed: {e!r}"


async def _reprobe_and_persist(media_file_id: int, path: str) -> None:
    """Re-probe the file at `path` (now the converted one) and refresh the
    MediaFile row so Convertarr's view of it matches reality."""
    try:
        probe = await ffprobe(path)
    except Exception as e:
        log.warning("re-probe failed for %s: %s", path, e)
        return
    plan = evaluate(probe, settings.policy)
    fmt = probe.get("format") or {}
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    size_bytes = int(fmt["size"]) if fmt.get("size") else None
    with session_scope() as s:
        mf = s.get(MediaFile, media_file_id)
        if mf is None:
            return
        mf.probe_json = probe
        mf.duration_seconds = duration
        mf.size_bytes = size_bytes
        mf.last_probed_at = datetime.now(timezone.utc)
        mf.needs_conversion = plan.needs_conversion
        mf.reason = "; ".join(plan.reasons) if plan.reasons else None


async def _run_one_job(job_id: int) -> None:
    # Snapshot job + media_file + arr instance from DB. The job has already
    # been atomically transitioned to `running` by `_claim_next_job_id`, so
    # we just need to fill in the encoder/argv/log_path metadata. If something
    # else flipped the state since (e.g. user cancelled), bail.
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        if job.state != JobState.running:
            # Cancelled in the brief window after claim; nothing to do.
            return
        mf = s.get(MediaFile, job.media_file_id)
        if mf is None:
            job.state = JobState.failed
            job.error_tail = "media_file missing"
            job.finished_at = datetime.now(timezone.utc)
            return
        instance = s.get(ArrInstance, mf.arr_instance_id) if mf.arr_instance_id else None
        # Snapshot fields we need after leaving the session (the relationship
        # would lazy-load otherwise). Stored in a plain dict to avoid Detached
        # ORM objects.
        instance_snapshot = (
            {"id": instance.id, "kind": instance.kind, "base_url": instance.base_url,
             "api_key": instance.api_key, "name": instance.name}
            if instance else None
        )
        input_path = mf.path
        media_file_id = mf.id
        duration = mf.duration_seconds
        entity_id = mf.arr_entity_id
        plan = _file_plan_from_db(mf)
        total_frames = _total_frames_from_probe(mf.probe_json, duration)
        encoder = detect_best()
        output_path = output_path_for(input_path, settings.policy)
        argv = build_ffmpeg_args(plan, encoder, input_path, output_path, settings.policy)

        # State + started_at were set by _claim_next_job_id; just fill in
        # the post-claim planning fields.
        job.encoder = encoder.name
        job.ffmpeg_args = argv
        job.output_path = str(output_path)
        log_path = settings.absolute_data_dir / "logs" / f"job-{job.id}.log"
        job.log_path = str(log_path)

    log.info(
        "job %d starting: %s -> %s (%s, total_frames=%s)",
        job_id, input_path, output_path, encoder.name, total_frames,
    )

    def _persist_progress(_p, pct: float) -> None:
        with session_scope() as s2:
            j = s2.get(Job, job_id)
            if j is None:
                return
            j.progress_pct = pct
            j.progress_speed = _p.speed
            j.progress_fps = _p.fps

    try:
        result = await runner.run(
            job_id, argv, Path(log_path), duration,
            on_progress=_persist_progress, total_frames=total_frames,
        )
    except Exception as e:
        log.exception("ffmpeg crashed for job %d", job_id)
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j is not None:
                j.state = JobState.failed
                j.error_tail = repr(e)
                j.finished_at = datetime.now(timezone.utc)
        return

    success = result.returncode == 0 and Path(output_path).exists() and Path(output_path).stat().st_size > 0

    # Was a cancel requested while we were running? If so, treat as cancelled
    # regardless of returncode (ffmpeg returns ~255 after SIGTERM).
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            return
        j.finished_at = datetime.now(timezone.utc)
        was_cancelled = j.state == JobState.cancelling

        if was_cancelled:
            j.state = JobState.cancelled
            j.error_tail = "cancelled by user"
        elif success:
            j.state = JobState.done
            j.progress_pct = 100.0
        else:
            j.state = JobState.failed
            j.error_tail = result.stderr_tail[-4000:]

    if was_cancelled:
        # Cleanup the partial output file so cancelled jobs don't leave half-encoded artifacts.
        try:
            Path(output_path).unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not delete partial output %s: %s", output_path, e)
        log.info("job %d cancelled", job_id)
    elif success:
        # 1. Swap converted file into the original's place; backup original to .old/
        # 2. Re-probe the new file so MediaFile reflects the new codec set
        # 3. Tell the *arr to refresh + rescan so its own DB picks up the change
        swap_ok, swap_err = _finalize_swap(Path(input_path), Path(output_path))
        if swap_ok:
            await _reprobe_and_persist(media_file_id, input_path)
            await _notify_arr_refresh_and_rescan(instance_snapshot, entity_id)
            log.info("job %d completed and finalized in place", job_id)
        else:
            with session_scope() as s:
                j = s.get(Job, job_id)
                if j is not None:
                    j.error_tail = swap_err
            log.warning("job %d encoded ok but finalize failed: %s", job_id, swap_err)
    else:
        log.error("job %d failed (rc=%d)", job_id, result.returncode)


def _claim_next_job_id() -> int | None:
    """Atomically transition the oldest queued job to `running`. Returns the
    job id we successfully claimed, or None if there are no queued jobs (or
    a parallel worker beat us to this one). The UPDATE's WHERE clause acts as
    the lock: if state changed between the SELECT and the UPDATE, rowcount is
    0 and we return None so the caller can poll again."""
    with session_scope() as s:
        jid = s.scalar(
            select(Job.id).where(Job.state == JobState.queued).order_by(Job.created_at).limit(1)
        )
        if jid is None:
            return None
        rows = s.execute(
            update(Job)
            .where(Job.id == jid, Job.state == JobState.queued)
            .values(state=JobState.running, started_at=datetime.now(timezone.utc))
        ).rowcount
        if rows == 0:
            return None
        return jid


_STALE_JOB_THRESHOLD_MIN = 30


def _reconcile_stuck_jobs() -> None:
    """When uvicorn is killed mid-job, the `running` / `cancelling` rows are
    orphaned — the ffmpeg subprocess they referred to is gone but the DB still
    thinks they're live. Sweep those up on startup.

    Only marks jobs as abandoned if they're definitively stuck (last update
    older than `_STALE_JOB_THRESHOLD_MIN` minutes). Recently-started jobs are
    left alone because another Convertarr process on the same DB might still
    own the encode — clobbering them would steal a successful job's result.
    """
    from datetime import timedelta
    from sqlalchemy import select as _select  # local to avoid import shuffling

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=_STALE_JOB_THRESHOLD_MIN)
    fixed = 0
    skipped_recent = 0
    with session_scope() as s:
        for j in s.scalars(_select(Job).where(Job.state.in_([JobState.running, JobState.cancelling]))).all():
            # Use the most recent of started_at / created_at as the activity
            # signal. If the row is recent we have no way to know if another
            # process is still encoding it — leave it.
            last_activity = j.started_at or j.created_at
            if last_activity and last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            if last_activity and last_activity > cutoff:
                skipped_recent += 1
                continue
            j.state = JobState.cancelled if j.state == JobState.cancelling else JobState.failed
            j.error_tail = "abandoned: server restart while running"
            j.finished_at = datetime.now(timezone.utc)
            fixed += 1
    if fixed:
        log.warning("reconciled %d stuck job(s) at startup", fixed)
    if skipped_recent:
        log.info("skipped %d recent running job(s) — another process may still own them", skipped_recent)


def _read_max_concurrency() -> int:
    """Read the `max_concurrent_jobs` setting fresh each loop tick so the user
    can change it from the UI without a restart. Clamped to [1, 16]."""
    from ..web import runtime_settings as rs
    try:
        n = int(rs.get("max_concurrent_jobs", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, min(16, n))


async def worker_loop(stop: asyncio.Event) -> None:
    """Pool-based worker. Maintains up to `max_concurrent_jobs` running encode
    tasks, claims new queued jobs as slots free up. Dynamic — if the user
    changes the setting mid-flight, the next poll tick observes the new value:
    larger → more jobs spawned immediately; smaller → in-flight jobs finish
    naturally and no new ones start until under limit."""
    log.info("worker loop started")
    _reconcile_stuck_jobs()
    running: dict[int, asyncio.Task] = {}
    stop_task = asyncio.create_task(stop.wait(), name="convertarr-worker-stop")
    try:
        while not stop.is_set():
            # Reap completed tasks
            for jid in list(running):
                if running[jid].done():
                    t = running.pop(jid)
                    exc = t.exception()
                    if exc is not None:
                        log.error("worker task for job %d crashed: %r", jid, exc)

            max_concurrent = _read_max_concurrency()

            # Fill empty slots with newly-claimed jobs
            while len(running) < max_concurrent:
                jid = _claim_next_job_id()
                if jid is None:
                    break
                running[jid] = asyncio.create_task(
                    _run_one_job(jid), name=f"convertarr-job-{jid}"
                )
                log.info("dispatched job %d (%d/%d slots)", jid, len(running), max_concurrent)

            # Wake on: stop signal, any running task finishing, or poll timeout
            waiters = [stop_task, *running.values()]
            try:
                await asyncio.wait(
                    waiters,
                    timeout=settings.worker_poll_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except Exception:
                log.exception("worker_loop wait failed")
    finally:
        stop_task.cancel()
        if running:
            log.info("draining %d in-flight job(s) on shutdown", len(running))
            await asyncio.gather(*running.values(), return_exceptions=True)
        log.info("worker loop stopped")
