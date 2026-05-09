from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


_HAS_STDBUF = shutil.which("stdbuf") is not None


def _wrap_with_line_buffer(argv: list[str]) -> list[str]:
    """ffmpeg block-buffers its stdout when it's a pipe, which can delay
    `-progress pipe:1` output by tens of seconds. `stdbuf -oL` (GNU coreutils)
    forces the child's stdout to line-buffer instead, so each progress key=val
    line shows up immediately. Falls back to argv unchanged if stdbuf isn't on
    the host (rare on Linux)."""
    if not _HAS_STDBUF:
        return argv
    return ["stdbuf", "-oL", *argv]


@dataclass
class Progress:
    out_time_us: int = 0
    total_size: int = 0
    speed: float | None = None
    fps: float | None = None
    frame: int | None = None


@dataclass
class RunResult:
    returncode: int
    stderr_tail: str
    log_path: Path


def _parse_progress_line(line: str, p: Progress) -> Progress:
    if "=" not in line:
        return p
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    try:
        if key == "out_time_us" or key == "out_time_ms":
            # `out_time_us` and `out_time_ms` both hold microseconds in modern ffmpeg
            p.out_time_us = int(value)
        elif key == "total_size":
            p.total_size = int(value)
        elif key == "speed":
            # value like "2.31x" or "N/A"
            if value.endswith("x"):
                p.speed = float(value[:-1])
        elif key == "fps":
            if value not in ("", "N/A"):
                p.fps = float(value)
        elif key == "frame":
            p.frame = int(value)
    except ValueError:
        pass
    return p


class Runner:
    """Owns the running ffmpeg subprocesses so the web layer can cancel them.

    Single-worker by design — one entry per job_id at a time. The worker calls
    `await runner.run(...)` and the cancel route calls `runner.cancel(job_id)`.
    """

    def __init__(self) -> None:
        self._procs: dict[int, asyncio.subprocess.Process] = {}

    async def run(
        self,
        job_id: int,
        argv: list[str],
        log_path: Path,
        duration_seconds: float | None,
        on_progress: Callable[[Progress, float], None] | None = None,
        total_frames: int | None = None,
    ) -> RunResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        wrapped = _wrap_with_line_buffer(argv)
        log.info("job %d: launching %s", job_id, " ".join(wrapped[:6]) + " ...")
        proc = await asyncio.create_subprocess_exec(
            *wrapped,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._procs[job_id] = proc

        progress = Progress()
        stderr_tail: list[str] = []
        tail_max = 200
        # Wall-clock anchor for our speed fallback. ffmpeg's `-progress`
        # stream emits `speed=N/A` intermittently for VAAPI (and other hwaccel
        # paths) — sometimes the entire encode never reports a usable speed.
        # We compute encoded_seconds / wall_seconds ourselves so the UI never
        # shows a blank speed pill just because ffmpeg was being coy.
        t_start = time.monotonic()

        async def pump_stdout() -> None:
            assert proc.stdout is not None
            line_count = 0
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    log.info("job %d: stdout EOF after %d lines", job_id, line_count)
                    return
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                line_count += 1
                _parse_progress_line(line, progress)
                # End of each progress block — fire the callback. Fewer DB writes,
                # same visible cadence (~2/s).
                if line.startswith("progress=") and on_progress is not None:
                    # Prefer frame-based pct: VAAPI emits `out_time_us=N/A`
                    # for muxed multi-stream output, but the frame counter
                    # always increments. Fall back to time-based if frames
                    # aren't available (e.g. when total_frames is unknown).
                    pct = 0.0
                    if total_frames and progress.frame:
                        pct = min(100.0, progress.frame / total_frames * 100.0)
                    elif duration_seconds and progress.out_time_us:
                        pct = min(100.0, (progress.out_time_us / 1_000_000.0) / duration_seconds * 100.0)
                    if progress.speed is None:
                        wall = time.monotonic() - t_start
                        encoded_s: float | None = None
                        if progress.out_time_us:
                            encoded_s = progress.out_time_us / 1_000_000.0
                        elif (
                            progress.frame
                            and total_frames
                            and duration_seconds
                        ):
                            encoded_s = progress.frame / total_frames * duration_seconds
                        if encoded_s is not None and wall > 0:
                            progress.speed = round(encoded_s / wall, 3)
                    log.debug(
                        "job %d progress: pct=%.2f speed=%s fps=%s frame=%s/%s",
                        job_id, pct, progress.speed, progress.fps,
                        progress.frame, total_frames,
                    )
                    on_progress(progress, pct)

        async def pump_stderr() -> None:
            assert proc.stderr is not None
            with log_path.open("ab") as fh:
                while True:
                    raw = await proc.stderr.readline()
                    if not raw:
                        return
                    fh.write(raw)
                    fh.flush()
                    stderr_tail.append(raw.decode(errors="replace"))
                    if len(stderr_tail) > tail_max:
                        del stderr_tail[: len(stderr_tail) - tail_max]

        try:
            await asyncio.gather(pump_stdout(), pump_stderr())
            rc = await proc.wait()
        finally:
            self._procs.pop(job_id, None)
        return RunResult(returncode=rc, stderr_tail="".join(stderr_tail), log_path=log_path)

    def cancel(self, job_id: int) -> bool:
        """Send SIGTERM to the running ffmpeg for `job_id`. Returns True if the
        process was found and signalled. ffmpeg cleans up and exits with rc != 0,
        which the worker maps to `cancelled` (not `failed`).
        """
        proc = self._procs.get(job_id)
        if proc is None:
            return False
        try:
            proc.terminate()
        except ProcessLookupError:
            return False
        log.info("sent SIGTERM to ffmpeg for job %d (pid=%s)", job_id, proc.pid)
        return True

    def is_running(self, job_id: int) -> bool:
        return job_id in self._procs


# Module-level singleton — one queue worker, one Runner.
runner = Runner()


# Backwards-compatible thin wrapper for any caller that still imports run_ffmpeg.
async def run_ffmpeg(
    argv: list[str],
    log_path: Path,
    duration_seconds: float | None,
    on_progress: Callable[[Progress, float], None] | None = None,
    total_frames: int | None = None,
) -> RunResult:
    return await runner.run(0, argv, log_path, duration_seconds, on_progress, total_frames=total_frames)
