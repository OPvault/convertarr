"""Shared job-execution core used by both the host's built-in `LocalWorker`
and the remote `convertarr-worker` CLI.

The boundary between host and worker is the `JobDispatch` payload — the host
hands one to a worker on `claim`, the worker runs ffmpeg + finalize_swap +
ffprobe locally, then ships back a `JobResult` on `finish`. JSON-serializable
shapes throughout so the payload is identical whether it's traveling
in-process (LocalWorker) or over HTTP (RemoteWorker).

What the host owns:
  - The DB queue and atomic claim
  - Path translation (using the target node's NodePathMapping rows)
  - FilePlan + Policy snapshot generation (so workers don't need DB access)
  - Recording finish results (state, probe, arr rescan trigger)

What the worker owns (whether local or remote):
  - Picking the encoder for its hardware
  - Building the final ffmpeg argv (encoder-specific flags)
  - Running ffmpeg, streaming progress, writing the log file
  - finalize_swap (replace original with output, with backup or delete)
  - Re-probing the new file
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update

from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..config import Policy, settings
from ..db import session_scope
from ..encode.hwdetect import EncoderProfile
from ..encode.plan import build_ffmpeg_args, output_path_for
from ..encode.runner import Runner
from ..models import ArrInstance, ArrKind, EntityIndex, Job, JobState, MediaFile, Node
from ..probe.ffprobe import ffprobe
from ..probe.policy import FilePlan, StreamPlan, evaluate, first_reencode_codec
from ..workflows import load_active_workflows, pick_workflow

log = logging.getLogger(__name__)


# Folder name used for the "keep originals" backup mode. Lives next to the
# source so the user can browse/restore from the same library tree.
BACKUP_DIR_NAME = ".convertarr-backup"


# ---- Dispatch payload (host → worker) ----

@dataclass
class JobDispatch:
    """Everything a worker needs to run one job. Constructed by the host on
    claim; fields are JSON-friendly so the same shape rides the wire to
    remote workers and lives in-memory for the local worker."""
    job_id: int
    media_file_id: int
    input_path: str               # already path-translated for the target node
    output_path: str
    log_path: str
    file_plan: dict               # FilePlan serialized (see _serialize_file_plan)
    policy: dict                  # Policy serialized via Pydantic model_dump()
    duration_seconds: float | None
    total_frames: int | None
    delete_originals: bool
    backup_dir_name: str
    # Denormalized so the worker can render its mirror Job row without
    # needing access to the host's MediaFile.
    display_title: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobDispatch":
        # Tolerate older host versions that don't send display_title.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---- Result payload (worker → host) ----

@dataclass
class JobResult:
    success: bool
    was_cancelled: bool = False
    returncode: int | None = None
    error_tail: str | None = None
    swap_ok: bool = True
    swap_err: str | None = None
    encoder_name: str | None = None
    ffmpeg_args: list[str] = field(default_factory=list)
    # Re-probed metadata after a successful encode (None on failure/cancel).
    new_probe: dict | None = None
    new_duration_seconds: float | None = None
    new_size_bytes: int | None = None
    # The actual paths the worker used. The host pre-translates on claim,
    # but the worker may apply further *arr PathMapping rewriting before
    # opening the file — the host stores these on the Job row so /history
    # reflects where the file actually lives, not the host's pre-translation.
    worker_input_path: str | None = None
    worker_output_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobResult":
        # Tolerate older worker versions missing the new fields.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# ---- FilePlan / Policy serialization ----

def _serialize_file_plan(plan: FilePlan) -> dict:
    return {
        "streams": [asdict(s) for s in plan.streams],
        "needs_conversion": plan.needs_conversion,
        "reasons": list(plan.reasons),
        "video_target_codec": plan.video_target_codec,
        "audio_target_codec": plan.audio_target_codec,
        "matched_workflow_id": plan.matched_workflow_id,
        "matched_workflow_name": plan.matched_workflow_name,
    }


def _deserialize_file_plan(data: dict) -> FilePlan:
    plan = FilePlan(
        streams=[StreamPlan(**s) for s in data.get("streams") or []],
        needs_conversion=bool(data.get("needs_conversion", False)),
        reasons=list(data.get("reasons") or []),
    )
    plan.video_target_codec = data.get("video_target_codec", "hevc")
    plan.audio_target_codec = data.get("audio_target_codec", "aac")
    plan.matched_workflow_id = data.get("matched_workflow_id")
    plan.matched_workflow_name = data.get("matched_workflow_name")
    return plan


def _serialize_policy(policy: Policy) -> dict:
    return policy.model_dump(mode="json")


def _deserialize_policy(data: dict) -> Policy:
    return Policy.model_validate(data)


# ---- Total-frame estimation (moved verbatim from queue.py) ----

def total_frames_from_probe(probe: dict | None, duration_seconds: float | None) -> int | None:
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


# ---- Path translation per-node ----
#
# Earlier versions of the multi-node feature kept a `NodePathMapping` table
# so the operator had to enter "host path → worker path" rules per worker.
# That duplicated information already in the worker's own *arr Path
# Mappings (since the worker is a fully-configured Convertarr instance
# with its own Sonarr/Radarr setup). We now ship the original *arr-
# relative path in the dispatch and let the worker translate via its own
# `arr/paths.translate` against its local PathMapping rows. This function
# remains as a no-op so older callers keep compiling, but it does nothing
# now that workers do their own translation on receive.


def apply_path_mappings(node_id: str, dispatch: JobDispatch) -> JobDispatch:
    """Deprecated: returns the dispatch unchanged. Workers now translate
    paths themselves using their own *arr PathMapping rows on receive,
    so the host doesn't need to pre-translate per-node."""
    return dispatch


# ---- Atomic claim ----

def claim_for_node(node_id: str) -> JobDispatch | None:
    """Atomically transition the oldest queued job to `running`, attribute it
    to `node_id`, and build a JobDispatch. Returns None if no queued jobs.

    Same race-safe SELECT-then-UPDATE pattern as the legacy
    `_claim_next_job_id`; the WHERE clause on UPDATE acts as the lock.
    """
    from ..web import runtime_settings as rs

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        jid = s.scalar(
            select(Job.id).where(Job.state == JobState.queued).order_by(Job.created_at).limit(1)
        )
        if jid is None:
            return None
        rows = s.execute(
            update(Job)
            .where(Job.id == jid, Job.state == JobState.queued)
            .values(state=JobState.running, started_at=now, node_id=node_id)
        ).rowcount
        if rows == 0:
            return None

        # Build the dispatch payload while we still hold the session.
        job = s.get(Job, jid)
        if job is None:
            return None
        mf = s.get(MediaFile, job.media_file_id)
        if mf is None:
            # Mark the job failed in this same transaction so we don't leak
            # a `running` row when the media row vanished out from under us.
            job.state = JobState.failed
            job.error_tail = "media_file missing"
            job.finished_at = now
            return None

        probe = mf.probe_json or {}
        match = pick_workflow(probe, load_active_workflows(), path=mf.path)
        policy = settings.policy
        plan = evaluate(probe, policy, workflow=match)
        # Local node uses the host's already-translated path (it's on the
        # same filesystem). Remote nodes receive the *arr-relative path so
        # they can apply their own Sonarr/Radarr PathMapping rows. Falls
        # back to mf.path for legacy rows that pre-date the column.
        if node_id == "local":
            input_path = mf.path
        else:
            input_path = mf.arr_original_path or mf.path
        output_path = str(output_path_for(input_path, policy))
        log_path = str(settings.absolute_data_dir / "logs" / f"job-{jid}.log")
        duration = mf.duration_seconds
        total_frames = total_frames_from_probe(probe, duration)
        delete_originals = bool(rs.get("delete_originals", True))

        # Persist log_path + output_path on the row so the queue/history
        # views can link to them even before the worker reports finish.
        # We persist the host's view of these paths (mf.path-derived), not
        # the *arr-relative one we may have just put in the dispatch — the
        # host's UI references should match what the host can read.
        host_output = str(output_path_for(mf.path, policy))
        host_log = log_path
        job.log_path = host_log
        job.output_path = host_output
        # Codec snapshot — drives the dashboard's "AV1 → HEVC" chip.
        # Stamped here (rather than computed at render time) so the 2-second
        # poll never has to re-evaluate the workflow.
        job.source_video_codec = first_reencode_codec(plan, "video")
        job.source_audio_codec = first_reencode_codec(plan, "audio")
        job.target_video_codec = plan.video_target_codec
        job.target_audio_codec = plan.audio_target_codec

        display_title = mf.arr_entity_title or Path(mf.path).name
        return JobDispatch(
            job_id=jid,
            media_file_id=mf.id,
            input_path=input_path,
            output_path=output_path,
            log_path=log_path,
            file_plan=_serialize_file_plan(plan),
            policy=_serialize_policy(policy),
            duration_seconds=duration,
            total_frames=total_frames,
            delete_originals=delete_originals,
            backup_dir_name=BACKUP_DIR_NAME,
            display_title=display_title,
        )


# ---- Per-job start metadata ----

def record_job_start(
    job_id: int,
    encoder_name: str,
    ffmpeg_args: list[str],
    source_path: str | None = None,
    output_path: str | None = None,
) -> None:
    """Stamp the encoder + final ffmpeg argv on the Job row right when the
    encode begins. The dashboard's "Now encoding" cards read these — without
    them, running jobs render with empty encoder pills and no argv link in
    queue history. Called by both the LocalWorker (direct DB write) and the
    remote `convertarr-worker` (via the /start API endpoint, which calls
    this same function on the host's process).

    `source_path` / `output_path` are the paths the worker actually opens
    on its own filesystem — for remote nodes those differ from the host's
    `mf.path` because mounts are laid out differently. Storing them on the
    Job row means /queue and /history can show a path the operator can
    actually reach on the worker, instead of the host's irrelevant view.
    """
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            return
        if encoder_name:
            j.encoder = encoder_name
        if ffmpeg_args:
            j.ffmpeg_args = ffmpeg_args
        if source_path:
            j.source_path = source_path
        if output_path:
            j.output_path = output_path


# ---- Per-job progress write ----

def update_progress(job_id: int, progress_pct: float, speed: float | None, fps: float | None) -> bool:
    """Write the latest progress sample to the DB. Returns True if the job is
    still in `running` state (i.e. not cancelling or already terminal) — the
    worker can use this as a fast-path cancel signal between heartbeats.
    """
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            return False
        if j.state == JobState.running:
            j.progress_pct = progress_pct
            j.progress_speed = speed
            j.progress_fps = fps
            return True
        return False  # cancelling / cancelled / done / failed


# ---- finalize_swap ----

def finalize_swap(input_path: Path, output_path: Path, *, delete_originals: bool,
                  backup_dir_name: str = BACKUP_DIR_NAME) -> tuple[bool, str | None]:
    """Replace the original file with the converted one. Default behavior
    deletes the original after the new file is in place; opting out moves it
    to `<dir>/<backup_dir_name>/<name>` instead of removing it.

    Pure local-FS operation — every node calls this on its own filesystem
    after a successful encode. Returns (ok, error_message).
    """
    try:
        if delete_originals:
            input_path.unlink(missing_ok=True)
            log.warning("DELETED original (delete_originals=true): %s", input_path)
        else:
            backup_dir = input_path.parent / backup_dir_name
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


# ---- Execute one dispatch ----

async def execute_dispatch(
    dispatch: JobDispatch,
    encoder: EncoderProfile,
    runner: Runner,
) -> JobResult:
    """Run one job from start to finish on the local filesystem. Called by
    both LocalWorker (in-process) and the remote convertarr-worker. The
    dispatch's paths are already translated for this node's view of the
    filesystem; this function never reaches across machines.

    `runner` is the per-process Runner singleton — local and remote each
    have their own so cancellations can SIGTERM the right subprocess.
    """
    plan = _deserialize_file_plan(dispatch.file_plan)
    policy = _deserialize_policy(dispatch.policy)
    input_path = Path(dispatch.input_path)
    output_path = Path(dispatch.output_path)
    log_path = Path(dispatch.log_path)

    argv = build_ffmpeg_args(plan, encoder, input_path, output_path, policy)

    # Stamp the encoder + argv on the row immediately so the dashboard's
    # "Now encoding" cards display them while the job is running (instead of
    # only at finish time, which left the encoder column showing "-").
    # Local worker writes directly; the remote worker has its own copy of
    # this code path but goes through the /start HTTP endpoint instead.
    record_job_start(
        dispatch.job_id, encoder.name, argv,
        source_path=str(input_path), output_path=str(output_path),
    )

    log.info(
        "job %d starting: %s -> %s (%s, total_frames=%s)",
        dispatch.job_id, input_path, output_path, encoder.name, dispatch.total_frames,
    )

    def _persist_progress(_p, pct: float) -> None:
        # Best-effort progress sink. LocalWorker writes directly to the DB
        # via update_progress; remote workers wrap this and POST instead.
        # The dispatch flow doesn't hard-require either to succeed — a
        # missing progress write isn't fatal, just unobservable.
        update_progress(dispatch.job_id, pct, _p.speed, _p.fps)

    # Allow the caller (RemoteWorker) to override progress via a wrapper if
    # they need to push to the host instead of writing locally. They do that
    # by re-binding `runner.run`'s `on_progress`. For LocalWorker we can use
    # the default `_persist_progress` since it talks to the same DB.
    try:
        result = await runner.run(
            dispatch.job_id, argv, log_path, dispatch.duration_seconds,
            on_progress=_persist_progress,
            total_frames=dispatch.total_frames,
        )
    except Exception as e:
        log.exception("ffmpeg crashed for job %d", dispatch.job_id)
        return JobResult(
            success=False, was_cancelled=False, returncode=None,
            error_tail=repr(e), encoder_name=encoder.name, ffmpeg_args=argv,
        )

    success = (
        result.returncode == 0
        and output_path.exists()
        and output_path.stat().st_size > 0
    )

    # Cancellation isn't deduced from returncode (ffmpeg returns ~255 after
    # SIGTERM); the caller knows whether it asked for a cancel. We probe the
    # DB row's state here as a hint — if the cancel route flipped the state
    # to `cancelling` while we were running, treat as cancelled.
    was_cancelled = False
    with session_scope() as s:
        j = s.get(Job, dispatch.job_id)
        if j is not None and j.state == JobState.cancelling:
            was_cancelled = True

    if was_cancelled:
        try:
            output_path.unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not delete partial output %s: %s", output_path, e)
        log.info("job %d cancelled", dispatch.job_id)
        return JobResult(
            success=False, was_cancelled=True, returncode=result.returncode,
            error_tail="cancelled by user", encoder_name=encoder.name, ffmpeg_args=argv,
            worker_input_path=str(input_path), worker_output_path=str(output_path),
        )

    if not success:
        log.error("job %d failed (rc=%d)", dispatch.job_id, result.returncode)
        return JobResult(
            success=False, was_cancelled=False, returncode=result.returncode,
            error_tail=result.stderr_tail[-4000:],
            encoder_name=encoder.name, ffmpeg_args=argv,
            worker_input_path=str(input_path), worker_output_path=str(output_path),
        )

    # Successful encode → swap, then re-probe the new file so the host's
    # MediaFile row reflects reality on the next claim cycle.
    swap_ok, swap_err = finalize_swap(
        input_path, output_path,
        delete_originals=dispatch.delete_originals,
        backup_dir_name=dispatch.backup_dir_name,
    )
    new_probe = None
    new_duration = None
    new_size = None
    if swap_ok:
        try:
            new_probe = await ffprobe(str(input_path))
            fmt = (new_probe or {}).get("format") or {}
            if fmt.get("duration"):
                try:
                    new_duration = float(fmt["duration"])
                except (TypeError, ValueError):
                    pass
            if fmt.get("size"):
                try:
                    new_size = int(fmt["size"])
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            log.warning("re-probe after encode failed for %s: %s", input_path, e)

    return JobResult(
        success=True, was_cancelled=False, returncode=result.returncode,
        error_tail=None, swap_ok=swap_ok, swap_err=swap_err,
        encoder_name=encoder.name, ffmpeg_args=argv,
        new_probe=new_probe, new_duration_seconds=new_duration, new_size_bytes=new_size,
        worker_input_path=str(input_path), worker_output_path=str(output_path),
    )


# ---- Record finish (host-side; updates DB + triggers arr rescan) ----

async def _notify_arr_refresh_and_rescan(instance: dict | None, entity_id: int | None) -> None:
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


async def record_finish(node_id: str, job_id: int, result: JobResult) -> None:
    """Apply a JobResult to the DB and (on success) trigger the arr rescan
    asynchronously. Idempotent on the state machine — if the job is already
    in a terminal state for some reason (e.g. the watchdog reset it because
    a heartbeat lapsed), we leave it alone.
    """
    instance_snapshot: dict | None = None
    arr_entity_id: int | None = None
    media_file_id: int | None = None
    new_probe = result.new_probe

    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            log.warning("record_finish: job %d not found", job_id)
            return

        # Don't overwrite a terminal state we set elsewhere (watchdog reset
        # to queued, manual cancel before we got here, etc.).
        if j.state not in (JobState.running, JobState.cancelling):
            log.info(
                "record_finish: job %d is %s — skipping result apply",
                job_id, j.state.value,
            )
            return

        if result.encoder_name:
            j.encoder = result.encoder_name
        if result.ffmpeg_args:
            j.ffmpeg_args = result.ffmpeg_args
        # Persist where the file actually was on the node that ran the
        # encode. For local jobs these match the host view; for remote
        # jobs they may differ (different mount layout) — /history reads
        # source_path/output_path so the operator sees the truth.
        if result.worker_input_path:
            j.source_path = result.worker_input_path
        if result.worker_output_path:
            j.output_path = result.worker_output_path
        # Snapshot node name so /history survives node rename/deletion.
        n = s.get(Node, node_id) if node_id else None
        if n is not None:
            j.node_name = n.name
        j.finished_at = datetime.now(timezone.utc)

        if result.was_cancelled:
            j.state = JobState.cancelled
            j.error_tail = result.error_tail or "cancelled by user"
        elif result.success:
            if not result.swap_ok:
                # Encode succeeded but the rename failed — surface as failed
                # so the user sees what's wrong. Original file is intact.
                j.state = JobState.failed
                j.error_tail = result.swap_err
            else:
                j.state = JobState.done
                j.progress_pct = 100.0
        else:
            j.state = JobState.failed
            j.error_tail = result.error_tail

        media_file_id = j.media_file_id
        mf = s.get(MediaFile, media_file_id) if media_file_id else None
        if mf is not None:
            arr_entity_id = mf.arr_entity_id
            inst = s.get(ArrInstance, mf.arr_instance_id) if mf.arr_instance_id else None
            if inst is not None:
                instance_snapshot = {
                    "id": inst.id, "kind": inst.kind, "base_url": inst.base_url,
                    "api_key": inst.api_key, "name": inst.name,
                }
            # Apply the new probe metadata reported by the worker.
            if result.success and result.swap_ok and new_probe:
                from ..probe.policy import evaluate as _evaluate
                from ..workflows import (
                    load_active_workflows as _load_active_workflows,
                    pick_workflow as _pick_workflow,
                )
                match = _pick_workflow(new_probe, _load_active_workflows(), path=mf.path)
                plan = _evaluate(new_probe, settings.policy, workflow=match)
                mf.probe_json = new_probe
                if result.new_duration_seconds is not None:
                    mf.duration_seconds = result.new_duration_seconds
                if result.new_size_bytes is not None:
                    mf.size_bytes = result.new_size_bytes
                mf.last_probed_at = datetime.now(timezone.utc)
                mf.needs_conversion = plan.needs_conversion
                mf.reason = "; ".join(plan.reasons) if plan.reasons else None

                # Library filters merge EntityIndex (cached codec/format sets,
                # rebuilt every 30 min by the indexer) with the live MediaFile
                # probe. Without this delete, a file we just re-encoded from
                # av1 to hevc would still match a `video_codec contains av1`
                # filter until the next indexer pass repopulates the row.
                if mf.arr_entity_id and mf.arr_instance_id and mf.arr_kind:
                    s.execute(
                        delete(EntityIndex).where(
                            EntityIndex.arr_kind == mf.arr_kind,
                            EntityIndex.arr_instance_id == mf.arr_instance_id,
                            EntityIndex.arr_entity_id == mf.arr_entity_id,
                        )
                    )

    # Outbound HTTP to Sonarr/Radarr lives outside the session.
    if result.success and result.swap_ok and not result.was_cancelled:
        await _notify_arr_refresh_and_rescan(instance_snapshot, arr_entity_id)
        log.info("job %d finished and arr-notified (node=%s)", job_id, node_id)


# ---- Cancellation lookup (used by heartbeat handler) ----

def cancellations_for(node_id: str, running_ids: list[int]) -> list[int]:
    """Of the running_ids the worker reported, which have been flipped to
    `cancelling` by the user? Returned ids are what the worker should SIGTERM
    locally."""
    if not running_ids:
        return []
    with session_scope() as s:
        rows = s.scalars(
            select(Job.id).where(
                Job.id.in_(running_ids),
                Job.state == JobState.cancelling,
                Job.node_id == node_id,
            )
        ).all()
        return list(rows)


# ---- Stuck-job reconciliation (startup) ----

def reconcile_stuck_jobs(stale_threshold_minutes: int = 30) -> None:
    """When uvicorn is killed mid-job, the `running` / `cancelling` rows are
    orphaned — the ffmpeg subprocess they referred to is gone. Sweep those up
    on startup. Mirrors the legacy behavior in queue.py.
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_threshold_minutes)
    fixed = 0
    skipped_recent = 0
    with session_scope() as s:
        for j in s.scalars(
            select(Job).where(Job.state.in_([JobState.running, JobState.cancelling]))
        ).all():
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
