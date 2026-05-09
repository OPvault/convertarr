"""Remote worker loop — register, heartbeat, claim, run, report.

Mirrors `local_node.local_worker_loop` but every interaction with the host is
HTTP-mediated. The actual encode + finalize_swap + reprobe runs locally on
this machine via the shared `workers.execution.execute_dispatch` path, just
with a progress callback that posts to the host instead of writing to a DB.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from ..arr.paths import Mapping, translate
from ..config import Policy
from ..db import session_scope
from ..encode.hwdetect import detect_best
from ..encode.plan import build_ffmpeg_args
from ..encode.runner import Runner
from ..models import Job, JobState
from ..probe.ffprobe import ffprobe
from ..probe.policy import FilePlan, StreamPlan
from ..workers.execution import (
    BACKUP_DIR_NAME,
    JobDispatch,
    JobResult,
    finalize_swap,
)
from .client import WorkerClient

log = logging.getLogger(__name__)


HEARTBEAT_INTERVAL_SECONDS = 15
CLAIM_POLL_SECONDS = 5            # how often to ask for new work when there's slack
RECONNECT_BACKOFF_SECONDS = 10
PROGRESS_THROTTLE_SECONDS = 1.0
# When the host repeatedly rejects us (401, network errors), back off so we
# don't spam ~17k requests/day at a misconfigured pairing. Resets to base
# on the first successful call.
AUTH_FAIL_BACKOFF_MAX_SECONDS = 120


class WorkerLoop:
    def __init__(
        self,
        client: WorkerClient,
        node_id: str,
        node_name: str,
        max_jobs_fallback: int = 1,
    ) -> None:
        self.client = client
        self.node_id = node_id
        self.node_name = node_name
        # Used only when this worker has no local Node row to read from
        # (e.g. the standalone `convertarr-worker` CLI without a full DB).
        self._max_jobs_fallback = max(1, int(max_jobs_fallback))
        # Each worker process owns its own Runner instance — keys subprocesses
        # by job_id locally so cancellations from heartbeat responses can find
        # the right one to SIGTERM.
        self.runner = Runner()
        self._running: dict[int, asyncio.Task] = {}
        # Job ids we've explicitly cancelled so we can distinguish "ffmpeg
        # exited because we asked it to" from "ffmpeg failed on its own".
        self._cancel_requested: set[int] = set()
        # Consecutive failures count — when the host keeps 401-ing or
        # is unreachable, we back off polling so the host isn't pestered
        # ~12/min about a problem only the operator can fix.
        self._consecutive_failures = 0

    # ---- Lifecycle ----

    async def run_forever(self, stop: asyncio.Event) -> None:
        encoder = detect_best()
        await self._register(encoder)
        tasks = [
            asyncio.create_task(self._heartbeat_loop(stop), name="worker-heartbeat"),
            asyncio.create_task(self._dispatch_loop(stop), name="worker-dispatch"),
        ]
        try:
            await stop.wait()
        finally:
            log.info("worker shutting down — draining %d job(s)", len(self._running))
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._running:
                # Don't kill in-flight subprocesses; let them finish so we
                # report results honestly.
                await asyncio.gather(*self._running.values(), return_exceptions=True)
            await self.client.close()

    async def _register(self, encoder) -> None:
        from .. import __version__ as _convertarr_version
        max_jobs = self._read_local_max_jobs()
        payload = {
            "node_id": self.node_id,
            "name": self.node_name,
            "encoder_family": encoder.family,
            "encoder_name": encoder.name,
            "max_concurrent_jobs": max_jobs,
            "version": _convertarr_version,
        }
        # First register attempt blocks startup so failures are visible to
        # the operator. After that, transient failures retry on the next tick.
        while True:
            try:
                await self.client.register(payload)
                log.info(
                    "registered with host: node_id=%s family=%s max=%d",
                    self.node_id, encoder.family, max_jobs,
                )
                return
            except Exception as e:
                log.warning("register failed: %r — retrying in %ds",
                            e, RECONNECT_BACKOFF_SECONDS)
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)

    def _read_local_max_jobs(self) -> int:
        """The worker's `max_concurrent_jobs` is owned by the worker — set on
        this machine's own /settings/nodes for the local Node row. Read fresh
        each tick so a UI change takes effect without restarting the worker.
        Clamped [1, 16]."""
        from ..models import Node
        from ..workers.local_node import LOCAL_NODE_ID
        try:
            with session_scope() as s:
                node = s.get(Node, LOCAL_NODE_ID)
                n = node.max_concurrent_jobs if node else self._max_jobs_fallback
        except Exception:
            n = self._max_jobs_fallback
        try:
            n = int(n)
        except (TypeError, ValueError):
            n = 1
        return max(1, min(16, n))

    # ---- Local Job mirror writes ----
    #
    # The worker's own DB gets a Job row for every job it runs for the host.
    # Without this, the worker's /dashboard, /history, and /system/statistics
    # are empty while it's actively encoding — the host owns the canonical
    # Job table, but the worker's UI has no visibility into it. The mirror
    # row is keyed on (host_job_id, node_id="local"); media_file_id stays
    # null because there's no local MediaFile to point at.

    def _create_mirror_job(self, dispatch: JobDispatch) -> int | None:
        """Insert a local mirror Job in `running` state. Returns its local
        id so subsequent progress/finish writes can target it. Returns
        None if the insert fails — the encode itself still proceeds; we
        just lose worker-side observability for this job."""
        try:
            with session_scope() as s:
                row = Job(
                    media_file_id=None,
                    state=JobState.running,
                    node_id="local",
                    host_job_id=dispatch.job_id,
                    display_title=dispatch.display_title or f"Job {dispatch.job_id}",
                    source_path=dispatch.input_path,
                    output_path=dispatch.output_path,
                    log_path=dispatch.log_path,
                    started_at=datetime.now(timezone.utc),
                )
                s.add(row)
                s.flush()
                return row.id
        except Exception as e:
            log.warning("failed to create mirror Job for host job %d: %r",
                        dispatch.job_id, e)
            return None

    def _update_mirror_progress(
        self, local_id: int | None, pct: float,
        speed: float | None, fps: float | None,
    ) -> None:
        if local_id is None:
            return
        try:
            with session_scope() as s:
                row = s.get(Job, local_id)
                if row is None or row.state != JobState.running:
                    return
                row.progress_pct = pct
                row.progress_speed = speed
                row.progress_fps = fps
        except Exception as e:
            log.debug("mirror progress write failed: %r", e)

    def _stamp_mirror_start(
        self, local_id: int | None, encoder_name: str, argv: list[str],
    ) -> None:
        if local_id is None:
            return
        try:
            with session_scope() as s:
                row = s.get(Job, local_id)
                if row is None:
                    return
                if encoder_name:
                    row.encoder = encoder_name
                if argv:
                    row.ffmpeg_args = list(argv)
        except Exception as e:
            log.debug("mirror start stamp failed: %r", e)

    def _finalize_mirror(self, local_id: int | None, result: JobResult) -> None:
        if local_id is None:
            return
        try:
            with session_scope() as s:
                row = s.get(Job, local_id)
                if row is None:
                    return
                if result.encoder_name:
                    row.encoder = result.encoder_name
                if result.ffmpeg_args:
                    row.ffmpeg_args = list(result.ffmpeg_args)
                if result.worker_input_path:
                    row.source_path = result.worker_input_path
                if result.worker_output_path:
                    row.output_path = result.worker_output_path
                row.finished_at = datetime.now(timezone.utc)
                if result.was_cancelled:
                    row.state = JobState.cancelled
                    row.error_tail = result.error_tail or "cancelled by user"
                elif result.success and result.swap_ok:
                    row.state = JobState.done
                    row.progress_pct = 100.0
                else:
                    row.state = JobState.failed
                    row.error_tail = result.error_tail or result.swap_err
        except Exception as e:
            log.warning("mirror finalize failed for local id %s: %r",
                        local_id, e)

    def _local_arr_mappings(self) -> list[Mapping]:
        """Walk every ArrInstance configured on THIS worker's local DB and
        flatten its PathMapping rows into the longest-prefix-match list the
        existing `arr/paths.translate` consumes. The host sends the *arr-
        relative path in the dispatch; the worker uses these to derive
        its own local view of the file."""
        from sqlalchemy import select
        from ..db import session_scope
        from ..models import ArrInstance
        out: list[Mapping] = []
        try:
            with session_scope() as s:
                for inst in s.scalars(select(ArrInstance)).all():
                    for m in inst.path_mappings:
                        out.append(Mapping(remote=m.remote_path, local=m.local_path))
        except Exception as e:
            log.warning("failed to load local arr path mappings: %r", e)
        return out

    # ---- Heartbeat ----

    async def _heartbeat_loop(self, stop: asyncio.Event) -> None:
        from .. import __version__ as _convertarr_version
        while not stop.is_set():
            try:
                resp = await self.client.heartbeat(
                    self.node_id, list(self._running.keys()),
                    max_concurrent_jobs=self._read_local_max_jobs(),
                    version=_convertarr_version,
                )
                for jid in resp.get("cancellations") or []:
                    jid_i = int(jid)
                    self._cancel_requested.add(jid_i)
                    if self.runner.cancel(jid_i):
                        log.info("cancellation honored for job %d", jid_i)
            except Exception as e:
                log.warning("heartbeat failed: %r", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    # ---- Claim + dispatch ----

    async def _dispatch_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            # Reap completed
            for jid in list(self._running):
                if self._running[jid].done():
                    t = self._running.pop(jid)
                    exc = t.exception()
                    if exc is not None:
                        log.error("worker job %d crashed: %r", jid, exc)

            # Refill empty slots — read max fresh each tick so UI changes
            # to the local node's max_concurrent_jobs take effect immediately.
            max_jobs = self._read_local_max_jobs()
            while len(self._running) < max_jobs:
                try:
                    resp = await self.client.claim(self.node_id)
                    self._consecutive_failures = 0  # reset on any success
                except Exception as e:
                    self._consecutive_failures += 1
                    log.warning("claim failed (#%d): %r",
                                self._consecutive_failures, e)
                    break
                payload = resp.get("job") if isinstance(resp, dict) else None
                if not payload:
                    break
                dispatch = JobDispatch.from_dict(payload)
                # Translate the *arr-relative paths in the dispatch into
                # this worker's local view. We use the worker's OWN
                # ArrInstance.path_mappings — same data the host's local
                # ingest uses on its side, just configured per-machine.
                local_mappings = self._local_arr_mappings()
                if local_mappings:
                    dispatch = JobDispatch(
                        job_id=dispatch.job_id,
                        media_file_id=dispatch.media_file_id,
                        input_path=translate(dispatch.input_path, local_mappings),
                        output_path=translate(dispatch.output_path, local_mappings),
                        log_path=translate(dispatch.log_path, local_mappings),
                        file_plan=dispatch.file_plan,
                        policy=dispatch.policy,
                        duration_seconds=dispatch.duration_seconds,
                        total_frames=dispatch.total_frames,
                        delete_originals=dispatch.delete_originals,
                        backup_dir_name=dispatch.backup_dir_name or BACKUP_DIR_NAME,
                    )
                local_id = self._create_mirror_job(dispatch)
                self._running[dispatch.job_id] = asyncio.create_task(
                    self._run_one(dispatch, local_id),
                    name=f"worker-job-{dispatch.job_id}",
                )
                log.info(
                    "claimed job %d (%d/%d slots, mirror=%s)",
                    dispatch.job_id, len(self._running), max_jobs,
                    local_id,
                )

            # Exponential-ish backoff while the host keeps rejecting us
            # (capped at AUTH_FAIL_BACKOFF_MAX_SECONDS). Resets to the base
            # CLAIM_POLL_SECONDS on the first successful call.
            poll = min(
                CLAIM_POLL_SECONDS * max(1, 2 ** min(self._consecutive_failures, 6)),
                AUTH_FAIL_BACKOFF_MAX_SECONDS,
            ) if self._consecutive_failures else CLAIM_POLL_SECONDS
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass

    # ---- Per-job execution ----

    async def _run_one(self, dispatch: JobDispatch, local_id: int | None) -> None:
        encoder = detect_best()
        loop = asyncio.get_event_loop()
        last_post = [0.0]

        async def _post_progress(payload: dict) -> None:
            try:
                resp = await self.client.progress(
                    self.node_id, dispatch.job_id, payload,
                )
                if resp.get("cancel"):
                    self._cancel_requested.add(dispatch.job_id)
                    self.runner.cancel(dispatch.job_id)
            except Exception as e:
                log.debug("progress post failed: %r", e)

        # Bridge the runner's sync callback into our async post. Throttled
        # so a fast encoder doesn't spam the host's API. The mirror row
        # gets the same sample so the worker's own dashboard ticks too.
        def _on_progress(_p, pct: float) -> None:
            now = loop.time()
            if now - last_post[0] < PROGRESS_THROTTLE_SECONDS:
                return
            last_post[0] = now
            self._update_mirror_progress(local_id, pct, _p.speed, _p.fps)
            asyncio.run_coroutine_threadsafe(
                _post_progress({
                    "progress_pct": pct,
                    "progress_speed": _p.speed,
                    "progress_fps": _p.fps,
                }),
                loop,
            )

        result = await self._encode_and_finalize(dispatch, encoder, _on_progress, local_id)
        self._finalize_mirror(local_id, result)
        try:
            await self.client.finish(self.node_id, dispatch.job_id, result.to_dict())
            log.info(
                "reported finish for job %d (success=%s cancelled=%s)",
                dispatch.job_id, result.success, result.was_cancelled,
            )
        except Exception as e:
            log.error("finish post failed for job %d: %r", dispatch.job_id, e)

    async def _encode_and_finalize(self, dispatch, encoder, on_progress, local_id) -> JobResult:
        """Worker-side equivalent of `execution.execute_dispatch`. Same shape;
        progress callback comes from the caller (so it can post over HTTP),
        and cancellation detection uses our local _cancel_requested set
        rather than the host DB (since the worker has no DB access).
        """
        plan = FilePlan(
            streams=[StreamPlan(**s) for s in dispatch.file_plan.get("streams") or []],
            needs_conversion=bool(dispatch.file_plan.get("needs_conversion", False)),
            reasons=list(dispatch.file_plan.get("reasons") or []),
        )
        plan.video_target_codec = dispatch.file_plan.get("video_target_codec", "hevc")
        plan.audio_target_codec = dispatch.file_plan.get("audio_target_codec", "aac")
        plan.matched_workflow_id = dispatch.file_plan.get("matched_workflow_id")
        plan.matched_workflow_name = dispatch.file_plan.get("matched_workflow_name")

        policy = Policy.model_validate(dispatch.policy)
        input_path = Path(dispatch.input_path)
        output_path = Path(dispatch.output_path)
        log_path = Path(dispatch.log_path)
        argv = build_ffmpeg_args(plan, encoder, input_path, output_path, policy)

        # Tell the host which encoder + argv we built so its dashboard can
        # display them while the job is running. Best-effort: a failure here
        # would only mean the encoder pill stays empty until /finish, so we
        # log + continue instead of aborting the job.
        try:
            await self.client.start(self.node_id, dispatch.job_id, {
                "encoder_name": encoder.name,
                "ffmpeg_args": argv,
                # Tell the host the path we actually opened on this
                # worker's filesystem (post local-arr translation), so the
                # host's queue/history shows a path the operator can
                # reach on the worker — not the host's own irrelevant view.
                "source_path": str(input_path),
                "output_path": str(output_path),
            })
        except Exception as e:
            log.warning("job %d /start post failed: %r", dispatch.job_id, e)
        # Mirror row gets the encoder + argv too so the worker's UI shows
        # them while ffmpeg is running, not only at finish.
        self._stamp_mirror_start(local_id, encoder.name, argv)

        log.info(
            "job %d starting: %s -> %s (%s, total_frames=%s)",
            dispatch.job_id, input_path, output_path, encoder.name,
            dispatch.total_frames,
        )

        try:
            result = await self.runner.run(
                dispatch.job_id, argv, log_path, dispatch.duration_seconds,
                on_progress=on_progress,
                total_frames=dispatch.total_frames,
            )
        except Exception as e:
            log.exception("ffmpeg crashed for job %d", dispatch.job_id)
            return JobResult(
                success=False, was_cancelled=False, returncode=None,
                error_tail=repr(e), encoder_name=encoder.name, ffmpeg_args=argv,
                worker_input_path=str(input_path), worker_output_path=str(output_path),
            )

        was_cancelled = dispatch.job_id in self._cancel_requested
        self._cancel_requested.discard(dispatch.job_id)

        if was_cancelled:
            try:
                output_path.unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete partial output %s: %s", output_path, e)
            return JobResult(
                success=False, was_cancelled=True, returncode=result.returncode,
                error_tail="cancelled by user", encoder_name=encoder.name, ffmpeg_args=argv,
                worker_input_path=str(input_path), worker_output_path=str(output_path),
            )

        success = (
            result.returncode == 0
            and output_path.exists()
            and output_path.stat().st_size > 0
        )
        if not success:
            return JobResult(
                success=False, was_cancelled=False, returncode=result.returncode,
                error_tail=result.stderr_tail[-4000:],
                encoder_name=encoder.name, ffmpeg_args=argv,
                worker_input_path=str(input_path), worker_output_path=str(output_path),
            )

        swap_ok, swap_err = finalize_swap(
            input_path, output_path,
            delete_originals=dispatch.delete_originals,
            backup_dir_name=dispatch.backup_dir_name or BACKUP_DIR_NAME,
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
            new_probe=new_probe, new_duration_seconds=new_duration,
            new_size_bytes=new_size,
            worker_input_path=str(input_path), worker_output_path=str(output_path),
        )
