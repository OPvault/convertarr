from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..arr.paths import Mapping, translate
from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..config import settings
from ..db import session_scope
from ..models import ArrInstance, ArrKind, Job, JobState, MediaFile, PathMapping
from ..probe.ffprobe import ffprobe
from ..probe.policy import evaluate

log = logging.getLogger(__name__)


async def _ingest_path(
    path: str,
    *,
    instance: ArrInstance,
    arr_entity_id: int,
    arr_entity_title: str,
    mappings: list[Mapping],
) -> int:
    """ffprobe the file, evaluate policy, upsert MediaFile, queue Job if needed.

    `path` is the path as returned by the *arr (i.e., from inside its container).
    It is translated through `mappings` before ffprobe so Convertarr can reach it
    on its own filesystem. The translated (local) path is what we persist and use
    for ffmpeg input.

    Returns the job id (or 0 if no conversion needed).
    """
    local_path = translate(path, mappings)
    if local_path != path:
        log.info("path translated: %s -> %s", path, local_path)
    path = local_path

    try:
        probe = await ffprobe(path)
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return 0

    plan = evaluate(probe, settings.policy)
    fmt = probe.get("format", {}) or {}
    duration = float(fmt["duration"]) if fmt.get("duration") else None
    size_bytes = int(fmt["size"]) if fmt.get("size") else None

    with session_scope() as s:
        mf = s.scalar(select(MediaFile).where(MediaFile.path == path))
        if mf is None:
            mf = MediaFile(path=path)
            s.add(mf)
        mf.arr_instance_id = instance.id
        mf.arr_kind = instance.kind
        mf.arr_entity_id = arr_entity_id
        mf.arr_entity_title = arr_entity_title
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


async def rescan_sonarr_series(instance_id: int, series_id: int) -> dict:
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

    queued = 0
    skipped = 0
    failed = 0
    for f in files:
        path = f.get("path")
        if not path:
            continue
        try:
            job_id = await _ingest_path(
                path,
                instance=instance_snapshot,
                arr_entity_id=series_id,
                arr_entity_title=title,
                mappings=mappings,
            )
            if job_id:
                queued += 1
            else:
                skipped += 1
        except Exception as e:
            log.exception("ingest failed for %s: %s", path, e)
            failed += 1

    return {"series_id": series_id, "title": title, "queued": queued, "skipped": skipped, "failed": failed}


async def rescan_sonarr_season(instance_id: int, series_id: int, season_number: int) -> dict:
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

    queued = 0
    skipped = 0
    failed = 0
    for f in files:
        if f.get("seasonNumber") != season_number:
            continue
        path = f.get("path")
        if not path:
            continue
        try:
            job_id = await _ingest_path(
                path,
                instance=instance_snapshot,
                arr_entity_id=series_id,
                arr_entity_title=title,
                mappings=mappings,
            )
            if job_id:
                queued += 1
            else:
                skipped += 1
        except Exception as e:
            log.exception("ingest failed for %s: %s", path, e)
            failed += 1

    return {
        "series_id": series_id,
        "season_number": season_number,
        "title": title,
        "queued": queued,
        "skipped": skipped,
        "failed": failed,
    }


async def rescan_sonarr_episode_file(instance_id: int, episode_file_id: int) -> dict:
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
    if series_id:
        try:
            series = await sonarr.get_series(series_id)
            title = series.get("title", title)
        except Exception:
            pass

    try:
        job_id = await _ingest_path(
            path,
            instance=instance_snapshot,
            arr_entity_id=series_id or 0,
            arr_entity_title=title,
            mappings=mappings,
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


async def rescan_radarr_movie(instance_id: int, movie_id: int) -> dict:
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
    )
    return {
        "movie_id": movie_id,
        "title": title,
        "queued": 1 if job_id else 0,
        "skipped": 0 if job_id else 1,
        "failed": 0,
    }
