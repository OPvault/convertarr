from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..arr.paths import Mapping, translate
from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..config import settings as app_settings
from ..db import session_scope
from ..models import ArrInstance, ArrKind, Job, JobState, MediaFile, PathMapping
from ..probe.ffprobe import ffprobe
from ..probe.policy import evaluate
from ..workflows import load_active_workflows, pick_workflow, pick_workflow_by_id

log = logging.getLogger(__name__)


# Bound concurrent ffprobes across the whole process. Each ingest spawns a
# subprocess + waits on disk I/O — sequential rescans of a 24-episode series
# took ~12 s; bumping to 8 in flight knocks that to under 2 s without
# overwhelming SQLite (WAL handles short writer queueing) or the network share.
_MAX_CONCURRENT_INGESTS = 8
_INGEST_SEMAPHORE: asyncio.Semaphore | None = None


def _ingest_semaphore() -> asyncio.Semaphore:
    """Lazy-init so the semaphore binds to whichever event loop the first
    rescan runs in. Module-level instantiation would lock to the loop
    that imported this module, which isn't always FastAPI's loop."""
    global _INGEST_SEMAPHORE
    if _INGEST_SEMAPHORE is None:
        _INGEST_SEMAPHORE = asyncio.Semaphore(_MAX_CONCURRENT_INGESTS)
    return _INGEST_SEMAPHORE


async def _ingest_one_guarded(coro_factory) -> tuple[int, Exception | None]:
    """Run a single _ingest_path call inside the semaphore. Returns
    (job_id, error) so callers can tally queued/skipped/failed without
    one bad file aborting the whole batch."""
    sem = _ingest_semaphore()
    async with sem:
        try:
            return (await coro_factory()) or 0, None
        except Exception as e:
            return 0, e


def _episode_number_map(episodes: list[dict]) -> dict[int, int]:
    """Build `episodeFileId -> episodeNumber`. Sonarr's `/episodefile`
    endpoint exposes `seasonNumber` but not `episodeNumber`; the per-episode
    join lives on `/episode`. Multi-episode files (rare) collapse to the
    lowest episode number — good enough for a dashboard label.
    """
    out: dict[int, int] = {}
    for ep in episodes or []:
        ef_id = ep.get("episodeFileId")
        ep_no = ep.get("episodeNumber")
        if not ef_id or ep_no is None:
            continue
        existing = out.get(ef_id)
        if existing is None or ep_no < existing:
            out[ef_id] = ep_no
    return out


async def _ingest_path(
    path: str,
    *,
    instance: ArrInstance,
    arr_entity_id: int,
    arr_entity_title: str,
    mappings: list[Mapping],
    workflow_id: int | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
) -> int:
    """ffprobe the file, evaluate policy, upsert MediaFile, queue Job if needed.

    `path` is the path as returned by the *arr (i.e., from inside its container).
    It is translated through `mappings` before ffprobe so Convertarr can reach it
    on its own filesystem. The translated (local) path is what we persist and use
    for ffmpeg input.

    `workflow_id` lets the rescan UI pin the operation to a specific user-chosen
    workflow (still respecting that workflow's conditions). When None, we walk
    every enabled workflow in priority order. Either way, if no workflow ends
    up matching, the file is recorded but no Job is queued — workflows are
    the only path to a conversion.

    Returns the job id (or 0 if no conversion needed).
    """
    # Remember the *arr-relative path BEFORE host translation. Remote
    # workers receive this verbatim and apply their own arr PathMapping
    # rows to derive their local view — no need for a separate node-level
    # mapping table. The host's local path (`local_path`) is what we'll
    # use for ffmpeg here on the host's own filesystem.
    arr_original_path = path
    local_path = translate(path, mappings)
    if local_path != path:
        log.info("path translated: %s -> %s", path, local_path)
    path = local_path

    try:
        probe = await ffprobe(path)
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return 0

    if workflow_id is not None:
        workflow_match = pick_workflow_by_id(probe, workflow_id, path=path)
    else:
        workflow_match = pick_workflow(probe, load_active_workflows(), path=path)
    plan = evaluate(probe, app_settings.policy, workflow=workflow_match)
    fmt = probe.get("format", {}) or {}
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    size_bytes = int(fmt["size"]) if fmt.get("size") else None

    with session_scope() as s:
        mf = s.scalar(select(MediaFile).where(MediaFile.path == path))
        if mf is None:
            mf = MediaFile(path=path)
            s.add(mf)
        mf.arr_original_path = arr_original_path
        mf.arr_instance_id = instance.id
        mf.arr_kind = instance.kind
        mf.arr_entity_id = arr_entity_id
        mf.arr_entity_title = arr_entity_title
        if instance.kind == ArrKind.sonarr:
            mf.season_number = season_number
            mf.episode_number = episode_number
        mf.size_bytes = size_bytes
        mf.duration_seconds = duration
        mf.probe_json = probe
        mf.last_probed_at = datetime.now(timezone.utc)
        mf.needs_conversion = plan.needs_conversion
        mf.reason = "; ".join(plan.reasons) if plan.reasons else None
        s.flush()

        if not plan.needs_conversion:
            return 0

        # Avoid double-queueing if there's already an active job for this file
        existing = s.scalar(
            select(Job).where(
                Job.media_file_id == mf.id,
                Job.state.in_([JobState.queued, JobState.running]),
            )
        )
        if existing is not None:
            return existing.id

        job = Job(media_file_id=mf.id, state=JobState.queued)
        s.add(job)
        s.flush()
        return job.id


async def rescan_sonarr_series(instance_id: int, series_id: int,
                                workflow_id: int | None = None) -> dict:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.sonarr:
            raise ValueError(f"Sonarr instance {instance_id} not found")
        client_args = (inst.base_url, inst.api_key)
        mappings = [Mapping(m.remote_path, m.local_path) for m in inst.path_mappings]
        # snapshot since we leave the session
        instance_snapshot = inst

    sonarr = SonarrClient(*client_args)
    series = await sonarr.get_series(series_id)
    title = series.get("title", f"series {series_id}")
    files = await sonarr.episode_files(series_id)
    ep_no_by_file = _episode_number_map(await sonarr.episodes(series_id))

    tasks = []
    paths = []
    for f in files:
        path = f.get("path")
        if not path:
            continue
        paths.append(path)
        tasks.append(_ingest_one_guarded(lambda f=f, p=path: _ingest_path(
            p,
            instance=instance_snapshot,
            arr_entity_id=series_id,
            arr_entity_title=title,
            mappings=mappings,
            workflow_id=workflow_id,
            season_number=f.get("seasonNumber"),
            episode_number=ep_no_by_file.get(f.get("id")),
        )))
    results = await asyncio.gather(*tasks)

    queued = 0
    skipped = 0
    failed = 0
    for path, (job_id, err) in zip(paths, results):
        if err is not None:
            log.exception("ingest failed for %s: %s", path, err)
            failed += 1
        elif job_id:
            queued += 1
        else:
            skipped += 1

    return {"series_id": series_id, "title": title, "queued": queued, "skipped": skipped, "failed": failed}


async def rescan_sonarr_season(instance_id: int, series_id: int, season_number: int,
                                workflow_id: int | None = None) -> dict:
    """Scan every episode file in a single season. Same shape as the whole-series
    rescan but pre-filters episodefile rows by `seasonNumber`."""
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.sonarr:
            raise ValueError(f"Sonarr instance {instance_id} not found")
        client_args = (inst.base_url, inst.api_key)
        mappings = [Mapping(m.remote_path, m.local_path) for m in inst.path_mappings]
        instance_snapshot = inst

    sonarr = SonarrClient(*client_args)
    series = await sonarr.get_series(series_id)
    title = series.get("title", f"series {series_id}")
    files = await sonarr.episode_files(series_id)
    ep_no_by_file = _episode_number_map(await sonarr.episodes(series_id))

    tasks = []
    paths = []
    for f in files:
        if f.get("seasonNumber") != season_number:
            continue
        path = f.get("path")
        if not path:
            continue
        paths.append(path)
        tasks.append(_ingest_one_guarded(lambda f=f, p=path: _ingest_path(
            p,
            instance=instance_snapshot,
            arr_entity_id=series_id,
            arr_entity_title=title,
            mappings=mappings,
            workflow_id=workflow_id,
            season_number=f.get("seasonNumber"),
            episode_number=ep_no_by_file.get(f.get("id")),
        )))
    results = await asyncio.gather(*tasks)

    queued = 0
    skipped = 0
    failed = 0
    for path, (job_id, err) in zip(paths, results):
        if err is not None:
            log.exception("ingest failed for %s: %s", path, err)
            failed += 1
        elif job_id:
            queued += 1
        else:
            skipped += 1

    return {
        "series_id": series_id,
        "season_number": season_number,
        "title": title,
        "queued": queued,
        "skipped": skipped,
        "failed": failed,
    }


async def rescan_sonarr_episode_file(instance_id: int, episode_file_id: int,
                                      workflow_id: int | None = None) -> dict:
    """Scan a single Sonarr episode file. Useful for the per-episode Rescan button."""
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.sonarr:
            raise ValueError(f"Sonarr instance {instance_id} not found")
        client_args = (inst.base_url, inst.api_key)
        mappings = [Mapping(m.remote_path, m.local_path) for m in inst.path_mappings]
        instance_snapshot = inst

    sonarr = SonarrClient(*client_args)
    f = await sonarr.episode_file(episode_file_id)
    path = f.get("path")
    if not path:
        return {"queued": 0, "skipped": 1, "failed": 0, "reason": "no path"}

    series_id = f.get("seriesId")
    title = f"series {series_id}"
    episode_number: int | None = None
    if series_id:
        try:
            series = await sonarr.get_series(series_id)
            title = series.get("title", title)
        except Exception:
            pass
        try:
            episode_number = _episode_number_map(
                await sonarr.episodes(series_id)
            ).get(episode_file_id)
        except Exception:
            pass

    try:
        job_id = await _ingest_path(
            path,
            instance=instance_snapshot,
            arr_entity_id=series_id or 0,
            arr_entity_title=title,
            mappings=mappings,
            workflow_id=workflow_id,
            season_number=f.get("seasonNumber"),
            episode_number=episode_number,
        )
    except Exception as e:
        log.exception("ingest failed for %s", path)
        return {"queued": 0, "skipped": 0, "failed": 1, "error": repr(e)}

    return {
        "queued": 1 if job_id else 0,
        "skipped": 0 if job_id else 1,
        "failed": 0,
        "job_id": job_id,
    }


async def rescan_radarr_movie(instance_id: int, movie_id: int,
                               workflow_id: int | None = None) -> dict:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.radarr:
            raise ValueError(f"Radarr instance {instance_id} not found")
        client_args = (inst.base_url, inst.api_key)
        mappings = [Mapping(m.remote_path, m.local_path) for m in inst.path_mappings]
        instance_snapshot = inst

    radarr = RadarrClient(*client_args)
    movie = await radarr.get_movie(movie_id)
    title = movie.get("title", f"movie {movie_id}")
    movie_file = movie.get("movieFile") or {}
    path = movie_file.get("path")

    if not path:
        return {"movie_id": movie_id, "title": title, "queued": 0, "skipped": 0, "failed": 0}

    job_id = await _ingest_path(
        path,
        instance=instance_snapshot,
        arr_entity_id=movie_id,
        arr_entity_title=title,
        mappings=mappings,
        workflow_id=workflow_id,
    )
    return {
        "movie_id": movie_id,
        "title": title,
        "queued": 1 if job_id else 0,
        "skipped": 0 if job_id else 1,
        "failed": 0,
    }
