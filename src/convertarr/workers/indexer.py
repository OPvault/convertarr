"""Background indexer for the series/movies grid filter UI.

The `format` and `video_codec` custom-filter fields are expensive to evaluate
live: for Sonarr they require a per-series `/episodefile?seriesId=X` round-trip
(N+1 across hundreds of series), and for Radarr they want a probe or a
mediaInfo parse. This worker walks every connected *arr instance on a fixed
cadence and stuffs the result into `entity_index`, so the request path is a
single indexed read instead of N HTTP calls.

Data sources, merged per entity:
  - MediaFile probe (most accurate; populated by `_ingest_path` after ffprobe)
  - Sonarr `episode_files()` mediaInfo / Radarr `movieFile.mediaInfo`
  - File extension from the path

The merge keeps codecs the indexer didn't see in this cycle if MediaFile still
has them, so a mid-rename doesn't blank a row.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..db import session_scope
from ..models import ArrInstance, ArrKind, EntityIndex, MediaFile
from ..web.filters import (
    _extract_extension,
    codecs_from_arr_mediainfo,
    video_codecs_from_probe,
)

log = logging.getLogger(__name__)


# 30 minutes between full re-indexes — Sonarr/Radarr libraries don't churn that
# fast, and per-series episode_files calls are the expensive piece. First pass
# runs immediately on startup so the index is warm shortly after launch.
INDEX_INTERVAL_SECONDS = 30 * 60

# Per-instance concurrency cap on Sonarr `/episodefile?seriesId=` calls. Same
# bound the live-fetch path used (see `_enrich_series_data`). Higher values
# pummel Sonarr without speeding things up much.
SONARR_FETCH_CONCURRENCY = 8


def _media_file_buckets_for_sonarr(instance_id: int) -> dict[int, dict[str, set[str]]]:
    """Group MediaFile rows under their owning series via path-prefix match.
    Same logic as `_enrich_series_data` but keyed by series id so the indexer
    can write one EntityIndex row per series."""
    with session_scope() as s:
        rows = s.execute(
            select(MediaFile.arr_entity_id, MediaFile.path, MediaFile.probe_json).where(
                MediaFile.arr_instance_id == instance_id,
                MediaFile.arr_kind == ArrKind.sonarr,
            )
        ).all()

    buckets: dict[int, dict[str, set[str]]] = {}
    for entity_id, path, probe in rows:
        if entity_id is None:
            continue
        b = buckets.setdefault(entity_id, {"formats": set(), "codecs": set()})
        if path:
            ext = _extract_extension(path)
            if ext:
                b["formats"].add(ext)
        for c in video_codecs_from_probe(probe or {}):
            b["codecs"].add(c)
    return buckets


def _media_file_buckets_for_radarr(instance_id: int) -> dict[int, dict[str, set[str]]]:
    with session_scope() as s:
        rows = s.execute(
            select(MediaFile.arr_entity_id, MediaFile.path, MediaFile.probe_json).where(
                MediaFile.arr_instance_id == instance_id,
                MediaFile.arr_kind == ArrKind.radarr,
            )
        ).all()

    buckets: dict[int, dict[str, set[str]]] = {}
    for entity_id, path, probe in rows:
        if entity_id is None:
            continue
        b = buckets.setdefault(entity_id, {"formats": set(), "codecs": set()})
        if path:
            ext = _extract_extension(path)
            if ext:
                b["formats"].add(ext)
        for c in video_codecs_from_probe(probe or {}):
            b["codecs"].add(c)
    return buckets


def _upsert_entity_index(
    arr_kind: ArrKind, instance_id: int,
    rows: list[tuple[int, list[str], list[str]]],
) -> None:
    """Bulk upsert. One transaction per instance keeps lock contention down on
    SQLite and makes a partial-failure mid-cycle visible as a missing row
    rather than a half-updated one."""
    if not rows:
        return
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        existing = {
            ei.arr_entity_id: ei
            for ei in s.scalars(
                select(EntityIndex).where(
                    EntityIndex.arr_kind == arr_kind,
                    EntityIndex.arr_instance_id == instance_id,
                )
            ).all()
        }
        for entity_id, formats, codecs in rows:
            ei = existing.get(entity_id)
            if ei is None:
                s.add(EntityIndex(
                    arr_kind=arr_kind,
                    arr_instance_id=instance_id,
                    arr_entity_id=entity_id,
                    formats=formats,
                    video_codecs=codecs,
                    updated_at=now,
                ))
            else:
                ei.formats = formats
                ei.video_codecs = codecs
                ei.updated_at = now


async def _index_sonarr_instance(instance_id: int, base_url: str, api_key: str) -> int:
    """Walk every series in this Sonarr instance, merge MediaFile probe data
    with the per-series episode_files response, and upsert one EntityIndex row
    per series. Returns the number of series indexed."""
    client = SonarrClient(base_url, api_key)
    try:
        series = await client.list_series()
    except Exception as e:
        log.warning("indexer: sonarr instance %d list_series failed: %r", instance_id, e)
        return 0

    mf_buckets = _media_file_buckets_for_sonarr(instance_id)
    sem = asyncio.Semaphore(SONARR_FETCH_CONCURRENCY)

    async def _index_series(sr: dict) -> tuple[int, list[str], list[str]] | None:
        sid = sr.get("id")
        if not sid:
            return None
        bucket = mf_buckets.get(sid, {"formats": set(), "codecs": set()})
        formats = set(bucket["formats"])
        codecs = set(bucket["codecs"])

        try:
            async with sem:
                episode_files = await client.episode_files(sid)
        except Exception as e:
            # Don't blow up the whole cycle on one flaky series; log + carry on
            # with whatever MediaFile data we already have.
            log.debug("indexer: sonarr episode_files(%d) failed: %r", sid, e)
            episode_files = []

        for ef in episode_files or []:
            ext = _extract_extension(ef.get("path") or "")
            if ext:
                formats.add(ext)
            mi = ef.get("mediaInfo") or {}
            for c in codecs_from_arr_mediainfo(mi.get("videoCodec")):
                codecs.add(c)
        return sid, sorted(formats), sorted(codecs)

    results = await asyncio.gather(*(_index_series(sr) for sr in series))
    rows = [r for r in results if r is not None]
    _upsert_entity_index(ArrKind.sonarr, instance_id, rows)
    return len(rows)


async def _index_radarr_instance(instance_id: int, base_url: str, api_key: str) -> int:
    """Walk every movie in this Radarr instance. Cheap: list_movies returns
    `movieFile.mediaInfo` inline, so this is one HTTP call per instance."""
    client = RadarrClient(base_url, api_key)
    try:
        movies = await client.list_movies()
    except Exception as e:
        log.warning("indexer: radarr instance %d list_movies failed: %r", instance_id, e)
        return 0

    mf_buckets = _media_file_buckets_for_radarr(instance_id)
    rows: list[tuple[int, list[str], list[str]]] = []
    for m in movies:
        mid = m.get("id")
        if not mid:
            continue
        bucket = mf_buckets.get(mid, {"formats": set(), "codecs": set()})
        formats = set(bucket["formats"])
        codecs = set(bucket["codecs"])

        mf = m.get("movieFile") or {}
        ext = _extract_extension(mf.get("path") or "")
        if ext:
            formats.add(ext)
        media_info = mf.get("mediaInfo") or {}
        for c in codecs_from_arr_mediainfo(media_info.get("videoCodec")):
            codecs.add(c)

        rows.append((mid, sorted(formats), sorted(codecs)))

    _upsert_entity_index(ArrKind.radarr, instance_id, rows)
    return len(rows)


async def index_all_instances() -> dict:
    """One full pass over every enabled *arr instance. Sequential across
    instances (Sonarr already parallelizes per-series internally) and safe to
    call from a route handler if we ever want a 'reindex now' button."""
    with session_scope() as s:
        instances = s.scalars(select(ArrInstance).where(ArrInstance.enabled.is_(True))).all()
        snapshots = [(i.id, i.kind, i.base_url, i.api_key) for i in instances]

    sonarr_indexed = 0
    radarr_indexed = 0
    for inst_id, kind, base_url, api_key in snapshots:
        try:
            if kind == ArrKind.sonarr:
                sonarr_indexed += await _index_sonarr_instance(inst_id, base_url, api_key)
            elif kind == ArrKind.radarr:
                radarr_indexed += await _index_radarr_instance(inst_id, base_url, api_key)
        except Exception:
            log.exception("indexer: instance %d (%s) failed", inst_id, kind)

    log.info("indexer pass complete: %d series, %d movies", sonarr_indexed, radarr_indexed)
    return {"series": sonarr_indexed, "movies": radarr_indexed}


async def indexer_loop(stop: asyncio.Event) -> None:
    """Run a full index pass on startup, then every INDEX_INTERVAL_SECONDS.
    Wakes early if `stop` is set so shutdown is responsive."""
    log.info("indexer loop started (interval %ds)", INDEX_INTERVAL_SECONDS)
    try:
        while not stop.is_set():
            try:
                await index_all_instances()
            except Exception:
                log.exception("indexer pass crashed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=INDEX_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("indexer loop stopped")
