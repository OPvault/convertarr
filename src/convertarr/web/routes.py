from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request

from .auth import require_auth
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..db import session_scope
from ..encode.hwdetect import detect_best, is_detected, list_known
from ..models import ArrInstance, ArrKind, ImageCache, Job, JobState, MediaFile, PathMapping, SavedFilter
from .filters import (
    BUILTIN_FILTERS,
    BUILTIN_LABELS,
    CUSTOM_FIELDS,
    CUSTOM_OPS,
    apply_filter,
    parse_filter_param,
)
from .sort import DEFAULT_DIR, DEFAULT_SORT, apply_sort, label_for, options_for
from ..workers.ingest import (
    rescan_radarr_movie,
    rescan_sonarr_episode_file,
    rescan_sonarr_season,
    rescan_sonarr_series,
)

router = APIRouter(dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ctx(active: str, **extra: object) -> dict:
    enc = detect_best()
    return {"active": active, "active_encoder_label": enc.label, **extra}


def _load_filter_options(scope: str) -> tuple[list[dict], list[dict]]:
    """Returns (builtin_options, saved_filters) for the dropdown."""
    builtin = [{"key": k, "label": v} for k, v in BUILTIN_LABELS.items()]
    with session_scope() as s:
        saved = s.scalars(
            select(SavedFilter).where(SavedFilter.scope == scope).order_by(SavedFilter.name)
        ).all()
        custom = [{"id": f.id, "name": f.name, "clauses": f.clauses or []} for f in saved]
    return builtin, custom


def _resolve_filter(spec: dict | None) -> dict | None:
    """Resolve a `parse_filter_param` result into something `apply_filter` can use,
    by loading saved filter clauses from the DB when needed."""
    if spec is None:
        return None
    if spec.get("kind") == "custom_id":
        with session_scope() as s:
            f = s.get(SavedFilter, spec["id"])
            if f is None:
                return None
            return {"kind": "custom", "clauses": f.clauses or []}
    return spec


def _enrich_movie_data(movies: list[dict], instance_id: int) -> None:
    """Attach `_formats` and `_video_codecs` to each movie in-place. Prefers
    local MediaFile probe data (most accurate, has every video stream); falls
    back to Radarr's `movieFile.mediaInfo.videoCodec` for movies Convertarr
    hasn't probed yet so the filter still works on a fresh library."""
    from .filters import _extract_extension, video_codecs_from_probe, codecs_from_arr_mediainfo
    if not movies:
        return
    with session_scope() as s:
        rows = s.execute(
            select(MediaFile.arr_entity_id, MediaFile.path, MediaFile.probe_json)
            .where(
                MediaFile.arr_instance_id == instance_id,
                MediaFile.arr_kind == ArrKind.radarr,
            )
        ).all()
    by_id: dict[int, dict[str, set[str]]] = {}
    for entity_id, path, probe in rows:
        if entity_id is None:
            continue
        bucket = by_id.setdefault(entity_id, {"formats": set(), "codecs": set()})
        ext = _extract_extension(path or "")
        if ext:
            bucket["formats"].add(ext)
        for c in video_codecs_from_probe(probe or {}):
            bucket["codecs"].add(c)
    for m in movies:
        mid = m.get("id")
        bucket = by_id.get(mid)
        mf = m.get("movieFile") or {}
        media_info = mf.get("mediaInfo") or {}

        formats: set[str] = set(bucket["formats"]) if bucket else set()
        codecs: set[str] = set(bucket["codecs"]) if bucket else set()

        # Always merge Radarr's reported codec — handy when MediaFile is empty,
        # harmless when it agrees with our probe.
        for c in codecs_from_arr_mediainfo(media_info.get("videoCodec")):
            codecs.add(c)
        if not formats:
            ext = _extract_extension(mf.get("path", ""))
            if ext:
                formats.add(ext)

        m["_formats"] = sorted(formats)
        m["_video_codecs"] = sorted(codecs)


def _filter_uses_field(spec: dict | None, field: str) -> bool:
    """True if `spec` is a custom filter whose clauses reference `field`. Used
    to gate expensive enrichment (per-series Sonarr fetch) so the listing
    stays snappy when no codec/format filter is active."""
    if not spec or spec.get("kind") != "custom":
        return False
    return any(c.get("field") == field for c in (spec.get("clauses") or []))


async def _enrich_series_data(
    series: list[dict],
    instance_id: int,
    client: "SonarrClient | None" = None,
    fetch_codecs: bool = False,
) -> None:
    """Attach `_formats` and `_video_codecs` to each series in-place.

    Two data sources, merged:
      1. Local MediaFile (path-prefix join) — accurate when files have been
         probed; gives us format + codec from ffprobe.
      2. Sonarr `/episodefile?seriesId=X` — only fetched when `fetch_codecs`
         is True (i.e. the user's filter actually needs codec data), since
         it's one HTTP call per series. Concurrent with Semaphore(8)."""
    import asyncio
    from .filters import _extract_extension, video_codecs_from_probe, codecs_from_arr_mediainfo
    if not series:
        return

    with session_scope() as s:
        rows = s.execute(
            select(MediaFile.path, MediaFile.probe_json).where(
                MediaFile.arr_instance_id == instance_id,
                MediaFile.arr_kind == ArrKind.sonarr,
            )
        ).all()
    files = [(p, probe) for p, probe in rows if p]

    # Longest series path first so nested libraries (e.g. /tv/Anime/Show vs
    # /tv/Show) attribute files to the deeper match.
    items = sorted(
        ((sr, sr.get("path") or "") for sr in series),
        key=lambda it: len(it[1]),
        reverse=True,
    )
    used: set[str] = set()
    buckets: dict[int, dict[str, set[str]]] = {}
    for sr, sp in items:
        bucket = {"formats": set(), "codecs": set()}
        if sp:
            sp_with_sep = sp.rstrip("/") + "/"
            for p, probe in files:
                if p in used:
                    continue
                if p.startswith(sp_with_sep) or p == sp:
                    ext = _extract_extension(p)
                    if ext:
                        bucket["formats"].add(ext)
                    for c in video_codecs_from_probe(probe or {}):
                        bucket["codecs"].add(c)
                    used.add(p)
        buckets[id(sr)] = bucket
    for sr in series:
        b = buckets.get(id(sr), {"formats": set(), "codecs": set()})
        sr["_formats"] = sorted(b["formats"])
        sr["_video_codecs"] = sorted(b["codecs"])

    if not (fetch_codecs and client):
        return

    sem = asyncio.Semaphore(8)

    async def _fill(sr: dict) -> None:
        sid = sr.get("id")
        if not sid:
            return
        try:
            async with sem:
                episode_files = await client.episode_files(sid)
        except Exception:
            return
        codecs = set(sr.get("_video_codecs") or [])
        formats = set(sr.get("_formats") or [])
        for ef in episode_files or []:
            mi = ef.get("mediaInfo") or {}
            for c in codecs_from_arr_mediainfo(mi.get("videoCodec")):
                codecs.add(c)
            ext = _extract_extension(ef.get("path") or "")
            if ext:
                formats.add(ext)
        sr["_video_codecs"] = sorted(codecs)
        sr["_formats"] = sorted(formats)

    await asyncio.gather(*(_fill(sr) for sr in series))


def _poster_url(images: list | None) -> str | None:
    """Pick a poster URL from a Sonarr/Radarr `images` array, prefer `remoteUrl`."""
    if not images:
        return None
    posters = [i for i in images if i.get("coverType") == "poster"]
    if not posters:
        posters = images
    poster = posters[0]
    return poster.get("remoteUrl") or poster.get("url")


def _dashboard_context() -> dict:
    """Snapshot of dashboard state, shared by the full page render and the
    HTMX-polled fragment so they always render the same rows."""
    with session_scope() as s:
        running = s.scalars(
            select(Job).where(Job.state.in_([JobState.running, JobState.cancelling])).order_by(Job.started_at)
        ).all()
        running_rows = [
            {
                "id": j.id,
                "state": j.state.value,
                "title": (s.get(MediaFile, j.media_file_id).arr_entity_title if j.media_file_id else "") or "",
                "progress": round(j.progress_pct, 1),
                "speed": j.progress_speed,
                "fps": j.progress_fps,
                "encoder": j.encoder,
                "eta": _format_eta(_eta_seconds(j.started_at, j.progress_pct)),
            }
            for j in running
        ]
        queued_count = len(s.scalars(select(Job).where(Job.state == JobState.queued)).all())
        done_count = len(s.scalars(select(Job).where(Job.state == JobState.done)).all())
        failed_count = len(s.scalars(select(Job).where(Job.state == JobState.failed)).all())
    return {
        "running": running_rows,
        "queued_count": queued_count,
        "done_count": done_count,
        "failed_count": failed_count,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx("dashboard", **_dashboard_context())
    )


@router.get("/dashboard/running", response_class=HTMLResponse)
async def dashboard_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_dashboard_running.html", _ctx("dashboard", **_dashboard_context())
    )


@router.get("/series", response_class=HTMLResponse)
async def series_page(
    request: Request,
    filter: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
) -> HTMLResponse:
    spec = _resolve_filter(parse_filter_param(filter))
    selected_filter = filter or "all"
    selected_sort = sort or DEFAULT_SORT["series"]
    selected_dir = dir if dir in ("asc", "desc") else DEFAULT_DIR

    with session_scope() as s:
        instances = s.scalars(
            select(ArrInstance).where(ArrInstance.kind == ArrKind.sonarr, ArrInstance.enabled.is_(True))
        ).all()
        instance_snapshots = [(i.id, i.name, i.base_url, i.api_key) for i in instances]

    groups: list[dict] = []
    for inst_id, name, base, key in instance_snapshots:
        client = SonarrClient(base, key)
        try:
            series = await client.list_series()
            error = None
        except Exception as e:
            series = []
            error = repr(e)
        # Per-series Sonarr fetch is expensive (N+1) so we only do it when the
        # filter actually references codec/format data. Keeps unfiltered loads
        # fast for users with hundreds of series.
        fetch_episode_files = (
            _filter_uses_field(spec, "video_codec")
            or _filter_uses_field(spec, "format")
        )
        await _enrich_series_data(series, inst_id, client, fetch_codecs=fetch_episode_files)
        # Filter, then sort the raw Sonarr response (so logic sees full fields).
        filtered = apply_filter(series, spec, kind="sonarr")
        ordered = apply_sort(filtered, selected_sort, selected_dir, scope="series")
        items = []
        for sr in ordered:
            items.append(
                {
                    "id": sr.get("id"),
                    "title": sr.get("title", ""),
                    "year": sr.get("year"),
                    "episode_file_count": (sr.get("statistics") or {}).get("episodeFileCount", 0),
                    "poster_proxy": f"/img/sonarr/{inst_id}?u={_poster_url(sr.get('images'))}" if _poster_url(sr.get("images")) else None,
                }
            )
        groups.append({
            "instance_id": inst_id, "instance_name": name,
            "entries": items, "error": error,
            "total": len(series), "shown": len(items),
        })

    builtin, custom = _load_filter_options("series")
    return templates.TemplateResponse(
        request,
        "series_list.html",
        _ctx("series",
             groups=groups,
             builtin_filters=builtin, custom_filters=custom, selected_filter=selected_filter,
             sort_options=options_for("series"), selected_sort=selected_sort, selected_dir=selected_dir,
             selected_sort_label=label_for("series", selected_sort),
             scope="series"),
    )


@router.post("/series/{instance_id}/{series_id}/rescan")
async def rescan_series(instance_id: int, series_id: int) -> dict:
    return await rescan_sonarr_series(instance_id, series_id)


@router.post("/series/bulk-rescan")
async def bulk_rescan_series(payload: dict) -> dict:
    """Fan out per-series rescans for the bulk-select UI. Each rescan probes +
    queues per policy, so this is the right primitive for "convert all selected"."""
    items = payload.get("items") or []
    if not items:
        return {"queued": 0, "skipped": 0, "failed": 0, "items": 0}
    results = await asyncio.gather(
        *(rescan_sonarr_series(int(i["instance_id"]), int(i["entity_id"])) for i in items),
        return_exceptions=True,
    )
    queued = skipped = failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            continue
        queued += r.get("queued", 0)
        skipped += r.get("skipped", 0)
        failed += r.get("failed", 0)
    return {"queued": queued, "skipped": skipped, "failed": failed, "items": len(items)}


@router.get("/series/{instance_id}/{series_id}", response_class=HTMLResponse)
async def series_detail(request: Request, instance_id: int, series_id: int) -> HTMLResponse:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.sonarr:
            raise HTTPException(404)
        base, key, name = inst.base_url, inst.api_key, inst.name

    client = SonarrClient(base, key)
    try:
        series = await client.get_series(series_id)
        episodes = await client.episodes(series_id)
        files = await client.episode_files(series_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Sonarr returned {e.response.status_code} for series {series_id}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach Sonarr: {e!r}")
    files_by_id = {f["id"]: f for f in files}

    seasons: dict[int, list[dict]] = {}
    for ep in sorted(episodes, key=lambda e: (e.get("seasonNumber", 0), e.get("episodeNumber", 0))):
        sn = ep.get("seasonNumber", 0)
        ef_id = ep.get("episodeFileId") or 0
        ef = files_by_id.get(ef_id) or {}
        media_info = ef.get("mediaInfo") or {}
        seasons.setdefault(sn, []).append(
            {
                "id": ep.get("id"),
                "season": sn,
                "number": ep.get("episodeNumber"),
                "title": ep.get("title", ""),
                "air_date": ep.get("airDate"),
                "has_file": bool(ep.get("hasFile")),
                "episode_file_id": ef_id or None,
                "video_codec": media_info.get("videoCodec"),
                "audio_codec": media_info.get("audioCodec"),
                "audio_channels": media_info.get("audioChannels"),
                "size_bytes": ef.get("size"),
                "path": ef.get("path"),
            }
        )

    detail = {
        "id": series.get("id"),
        "title": series.get("title"),
        "year": series.get("year"),
        "overview": series.get("overview", ""),
        "network": series.get("network"),
        "status": series.get("status"),
        "genres": series.get("genres", []),
        "poster_proxy": (
            f"/img/sonarr/{instance_id}?u={_poster_url(series.get('images'))}"
            if _poster_url(series.get("images"))
            else None
        ),
        "instance_id": instance_id,
        "instance_name": name,
        "stats": series.get("statistics", {}),
        # Sonarr default: highest season first, Specials (0) at the very end.
        "seasons": sorted(seasons.items(), key=lambda kv: (0, -kv[0]) if kv[0] != 0 else (1, 0)),
    }

    return templates.TemplateResponse(
        request, "series_detail.html", _ctx("series", series=detail)
    )


@router.post("/series/{instance_id}/episodefile/{episode_file_id}/rescan")
async def rescan_episode_file(instance_id: int, episode_file_id: int) -> dict:
    return await rescan_sonarr_episode_file(instance_id, episode_file_id)


@router.post("/series/{instance_id}/{series_id}/season/{season_number}/rescan")
async def rescan_season(instance_id: int, series_id: int, season_number: int) -> dict:
    return await rescan_sonarr_season(instance_id, series_id, season_number)


@router.get("/movies", response_class=HTMLResponse)
async def movies_page(
    request: Request,
    filter: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
) -> HTMLResponse:
    spec = _resolve_filter(parse_filter_param(filter))
    selected_filter = filter or "all"
    selected_sort = sort or DEFAULT_SORT["movies"]
    selected_dir = dir if dir in ("asc", "desc") else DEFAULT_DIR

    with session_scope() as s:
        instances = s.scalars(
            select(ArrInstance).where(ArrInstance.kind == ArrKind.radarr, ArrInstance.enabled.is_(True))
        ).all()
        instance_snapshots = [(i.id, i.name, i.base_url, i.api_key) for i in instances]

    groups: list[dict] = []
    for inst_id, name, base, key in instance_snapshots:
        try:
            movies = await RadarrClient(base, key).list_movies()
            error = None
        except Exception as e:
            movies = []
            error = repr(e)
        _enrich_movie_data(movies, inst_id)
        filtered = apply_filter(movies, spec, kind="radarr")
        ordered = apply_sort(filtered, selected_sort, selected_dir, scope="movies")
        items = []
        for m in ordered:
            items.append(
                {
                    "id": m.get("id"),
                    "title": m.get("title", ""),
                    "year": m.get("year"),
                    "has_file": bool(m.get("hasFile")),
                    "poster_proxy": f"/img/radarr/{inst_id}?u={_poster_url(m.get('images'))}" if _poster_url(m.get("images")) else None,
                }
            )
        groups.append({
            "instance_id": inst_id, "instance_name": name,
            "entries": items, "error": error,
            "total": len(movies), "shown": len(items),
        })

    builtin, custom = _load_filter_options("movies")
    return templates.TemplateResponse(
        request,
        "movies_list.html",
        _ctx("movies",
             groups=groups,
             builtin_filters=builtin, custom_filters=custom, selected_filter=selected_filter,
             sort_options=options_for("movies"), selected_sort=selected_sort, selected_dir=selected_dir,
             selected_sort_label=label_for("movies", selected_sort),
             scope="movies"),
    )


@router.post("/movies/{instance_id}/{movie_id}/rescan")
async def rescan_movie(instance_id: int, movie_id: int) -> dict:
    return await rescan_radarr_movie(instance_id, movie_id)


@router.post("/movies/bulk-rescan")
async def bulk_rescan_movies(payload: dict) -> dict:
    """Fan out per-movie rescans for the bulk-select UI."""
    items = payload.get("items") or []
    if not items:
        return {"queued": 0, "skipped": 0, "failed": 0, "items": 0}
    results = await asyncio.gather(
        *(rescan_radarr_movie(int(i["instance_id"]), int(i["entity_id"])) for i in items),
        return_exceptions=True,
    )
    queued = skipped = failed = 0
    for r in results:
        if isinstance(r, Exception):
            failed += 1
            continue
        queued += r.get("queued", 0)
        skipped += r.get("skipped", 0)
        failed += r.get("failed", 0)
    return {"queued": queued, "skipped": skipped, "failed": failed, "items": len(items)}


@router.get("/movies/{instance_id}/{movie_id}", response_class=HTMLResponse)
async def movie_detail(request: Request, instance_id: int, movie_id: int) -> HTMLResponse:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None or inst.kind != ArrKind.radarr:
            raise HTTPException(404)
        base, key, name = inst.base_url, inst.api_key, inst.name

    client = RadarrClient(base, key)
    try:
        movie = await client.get_movie(movie_id)
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Radarr returned {e.response.status_code} for movie {movie_id}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Couldn't reach Radarr: {e!r}")
    movie_file = movie.get("movieFile") or {}
    media_info = movie_file.get("mediaInfo") or {}

    detail = {
        "id": movie.get("id"),
        "title": movie.get("title"),
        "year": movie.get("year"),
        "overview": movie.get("overview", ""),
        "studio": movie.get("studio"),
        "runtime": movie.get("runtime"),
        "status": movie.get("status"),
        "genres": movie.get("genres", []),
        "poster_proxy": (
            f"/img/radarr/{instance_id}?u={_poster_url(movie.get('images'))}"
            if _poster_url(movie.get("images"))
            else None
        ),
        "instance_id": instance_id,
        "instance_name": name,
        "has_file": bool(movie.get("hasFile")),
        "file_path": movie_file.get("path"),
        "file_size": movie_file.get("size"),
        "video_codec": media_info.get("videoCodec"),
        "audio_codec": media_info.get("audioCodec"),
        "audio_channels": media_info.get("audioChannels"),
        "resolution": media_info.get("resolution"),
    }

    return templates.TemplateResponse(
        request, "movie_detail.html", _ctx("movies", movie=detail)
    )


_IMG_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=86400, immutable",
}


@router.get("/img/{kind}/{instance_id}")
async def image_proxy(kind: str, instance_id: int, u: str) -> Response:
    """Proxy poster images so we can authenticate to *arr without exposing the
    API key. Accepts either a fully-qualified URL (TheTVDB / TMDB CDN) or a
    Sonarr/Radarr relative path like /MediaCover/12/poster.jpg.

    Bytes are cached in the `image_cache` table keyed by the resolved URL, so
    we only fetch each poster from the source once.
    """
    if kind not in ("sonarr", "radarr"):
        raise HTTPException(404)
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None:
            raise HTTPException(404)
        base, key = inst.base_url, inst.api_key

    is_external = u.startswith(("http://", "https://"))
    target = u if is_external else f"{base}{u}"

    # Cache lookup first — typical case after first warmup
    with session_scope() as s:
        hit = s.get(ImageCache, target)
        if hit is not None:
            return Response(
                content=hit.content,
                media_type=hit.content_type,
                headers=_IMG_CACHE_HEADERS,
            )

    headers = {} if is_external else {"X-Api-Key": key}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(target, headers=headers)
            r.raise_for_status()
    except Exception:
        return Response(status_code=204)

    content_type = r.headers.get("content-type", "image/jpeg")
    body = r.content

    with session_scope() as s:
        # Use merge so concurrent requests for the same poster don't fail on PK collision.
        s.merge(ImageCache(url=target, content_type=content_type, content=body))

    return Response(content=body, media_type=content_type, headers=_IMG_CACHE_HEADERS)


@router.get("/api/filters")
async def list_filters(scope: str) -> dict:
    if scope not in ("series", "movies"):
        raise HTTPException(400, "scope must be 'series' or 'movies'")
    builtin, custom = _load_filter_options(scope)
    return {
        "builtin": builtin,
        "custom": custom,
        "fields": [
            {"key": k, "label": v["label"], "type": v["type"], "suggestions": v.get("suggestions") or []}
            for k, v in CUSTOM_FIELDS.items()
        ],
        "ops": [{"key": k, "label": v["label"], "applies_to": list(v["applies_to"])} for k, v in CUSTOM_OPS.items()],
    }


@router.post("/api/filters")
async def create_filter(payload: dict) -> dict:
    """Body: {scope: "series"|"movies", name: str, clauses: [{field, op, value}]}"""
    scope = payload.get("scope")
    name = (payload.get("name") or "").strip()
    clauses = payload.get("clauses") or []
    if scope not in ("series", "movies") or not name:
        raise HTTPException(400, "scope + non-empty name required")
    if not isinstance(clauses, list):
        raise HTTPException(400, "clauses must be a list")
    # Validate clauses minimally
    cleaned = []
    for c in clauses:
        if not isinstance(c, dict):
            continue
        if c.get("field") not in CUSTOM_FIELDS:
            continue
        if c.get("op") not in CUSTOM_OPS:
            continue
        cleaned.append({"field": c["field"], "op": c["op"], "value": str(c.get("value", ""))})
    with session_scope() as s:
        f = SavedFilter(scope=scope, name=name, clauses=cleaned)
        s.add(f)
        s.flush()
        return {"id": f.id, "scope": scope, "name": name, "clauses": cleaned}


@router.post("/api/filters/{filter_id}/delete")
async def delete_filter(filter_id: int) -> dict:
    with session_scope() as s:
        f = s.get(SavedFilter, filter_id)
        if f:
            s.delete(f)
    return {"ok": True}


@router.post("/settings/image-cache/flush")
async def flush_image_cache() -> dict:
    """Wipe the cached poster bytes — useful if a cover changed upstream."""
    with session_scope() as s:
        n = s.query(ImageCache).delete()
    return {"flushed": n}


@router.get("/queue", response_class=HTMLResponse)
async def queue_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "queue.html", _ctx("queue", rows=_active_jobs()))


def _eta_seconds(started_at, progress_pct: float | None) -> int | None:
    """Estimate seconds remaining from elapsed wall time and current %.
    Returns None when there isn't enough signal to be meaningful (job not
    started, or pct still under 1%). Uses elapsed * (100 - pct) / pct so it
    self-corrects as the encode runs — if early frames are slow and later
    ones are fast, the ETA tightens over time."""
    if started_at is None or progress_pct is None or progress_pct < 1:
        return None
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    if elapsed < 1:
        return None
    return int(elapsed * (100 - progress_pct) / progress_pct)


def _format_eta(seconds: int | None) -> str:
    """Compact human format: '45s', '2m 30s', '1h 12m'. None → em dash."""
    if seconds is None or seconds < 0:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _active_jobs() -> list[dict]:
    with session_scope() as s:
        active = s.scalars(
            select(Job).where(Job.state.in_([JobState.queued, JobState.running])).order_by(Job.created_at)
        ).all()
        rows = []
        for j in active:
            mf = s.get(MediaFile, j.media_file_id)
            eta = _eta_seconds(j.started_at, j.progress_pct)
            rows.append({
                "id": j.id,
                "state": j.state.value,
                "progress": round(j.progress_pct, 1),
                "speed": j.progress_speed,
                "fps": j.progress_fps,
                "encoder": j.encoder,
                "title": mf.arr_entity_title if mf else "",
                "path": mf.path if mf else "",
                "reason": mf.reason if mf else "",
                "eta": _format_eta(eta),
            })
    return rows


@router.get("/queue/fragment", response_class=HTMLResponse)
async def queue_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "_queue_rows.html", {"rows": _active_jobs()})


@router.post("/queue/cancel-all")
async def cancel_all_jobs() -> dict:
    """Cancel every queued and running job. Queued ones are marked cancelled
    directly; running ones are flagged cancelling and SIGTERM'd via the runner.
    Returns counts so the UI can show what happened."""
    from ..encode.runner import runner

    cancelled_queued = 0
    cancelling_running: list[int] = []
    with session_scope() as s:
        rows = s.scalars(
            select(Job).where(Job.state.in_([JobState.queued, JobState.running]))
        ).all()
        now = datetime.now(timezone.utc)
        for j in rows:
            if j.state == JobState.queued:
                j.state = JobState.cancelled
                j.finished_at = now
                j.error_tail = "cancelled before start"
                cancelled_queued += 1
            elif j.state == JobState.running:
                j.state = JobState.cancelling
                cancelling_running.append(j.id)

    signalled = 0
    for jid in cancelling_running:
        if runner.cancel(jid):
            signalled += 1
    return {
        "ok": True,
        "cancelled_queued": cancelled_queued,
        "cancelling_running": len(cancelling_running),
        "signalled": signalled,
    }


@router.post("/queue/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict:
    """Cancel a queued or running job. Queued jobs are marked cancelled directly;
    running jobs are flagged `cancelling` and SIGTERM'd via the runner registry —
    the worker promotes them to `cancelled` on exit."""
    from ..encode.runner import runner

    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            raise HTTPException(404)
        state = j.state
        if state == JobState.queued:
            j.state = JobState.cancelled
            j.finished_at = datetime.now(timezone.utc)
            j.error_tail = "cancelled before start"
            return {"ok": True, "state": "cancelled"}
        if state == JobState.running:
            j.state = JobState.cancelling

    if state == JobState.running:
        signalled = runner.cancel(job_id)
        return {"ok": True, "state": "cancelling", "signalled": signalled}
    return {"ok": False, "reason": f"job is {state.value}"}


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    with session_scope() as s:
        finished = s.scalars(
            select(Job)
            .where(Job.state.in_([JobState.done, JobState.failed, JobState.skipped]))
            .order_by(Job.finished_at.desc())
            .limit(200)
        ).all()
        rows = []
        for j in finished:
            mf = s.get(MediaFile, j.media_file_id)
            rows.append({
                "id": j.id,
                "state": j.state.value,
                "encoder": j.encoder,
                "title": mf.arr_entity_title if mf else "",
                "path": mf.path if mf else "",
                "output_path": j.output_path,
                "error_tail": j.error_tail,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
            })
    return templates.TemplateResponse(request, "history.html", _ctx("history", rows=rows))


async def _instance_snapshots_for(kind: ArrKind) -> list[dict]:
    with session_scope() as s:
        instances = s.scalars(
            select(ArrInstance).where(ArrInstance.kind == kind).order_by(ArrInstance.name)
        ).all()
        snaps = [
            {
                "id": i.id,
                "kind": i.kind.value,
                "name": i.name,
                "address": i.address,
                "port": i.port,
                "base_path": i.base_path or "/",
                "use_ssl": bool(i.use_ssl),
                "http_timeout": i.http_timeout,
                "base_url": i.base_url,
                "api_key": i.api_key,
                "enabled": i.enabled,
                "mappings": [
                    {"id": m.id, "remote": m.remote_path, "local": m.local_path}
                    for m in i.path_mappings
                ],
            }
            for i in instances
        ]

    for snap in snaps:
        client: SonarrClient | RadarrClient = (
            SonarrClient(snap["base_url"], snap["api_key"])
            if snap["kind"] == "sonarr"
            else RadarrClient(snap["base_url"], snap["api_key"])
        )
        try:
            folders = await client.list_root_folders()
            snap["remote_paths"] = sorted({f["path"] for f in folders if f.get("path")})
            snap["fetch_error"] = None
        except Exception as e:
            snap["remote_paths"] = []
            snap["fetch_error"] = repr(e)
        snap.pop("api_key")
    return snaps


@router.get("/settings", response_class=HTMLResponse)
async def settings_root(request: Request) -> RedirectResponse:
    """Backwards-compat: send any old bookmarks to the new General tab."""
    return RedirectResponse("/settings/general", status_code=302)


@router.get("/settings/general", response_class=HTMLResponse)
async def settings_general(request: Request) -> HTMLResponse:
    from . import runtime_settings as rs
    encoders = [
        {"name": p.name, "label": p.label, "family": p.family, "detected": is_detected(p)}
        for p in list_known()
    ]
    return templates.TemplateResponse(
        request,
        "settings_general.html",
        _ctx(
            "settings/general",
            bind_address=rs.get("bind_address", "0.0.0.0:8000"),
            auth_method=rs.get("auth_method", "none"),
            auth_username=rs.get("auth_username", "admin"),
            api_key=rs.get("api_key", ""),
            delete_originals=bool(rs.get("delete_originals", False)),
            encoders=encoders,
            encoder_choice=rs.get("encoder_choice", "auto"),
            active_encoder=detect_best(),
            max_concurrent_jobs=int(rs.get("max_concurrent_jobs", 1)),
        ),
    )


@router.get("/settings/sonarr", response_class=HTMLResponse)
async def settings_sonarr(request: Request) -> HTMLResponse:
    snaps = await _instance_snapshots_for(ArrKind.sonarr)
    return templates.TemplateResponse(
        request, "settings_sonarr.html",
        _ctx("settings/sonarr", instances=snaps, kind="sonarr", default_port=8989),
    )


@router.get("/settings/radarr", response_class=HTMLResponse)
async def settings_radarr(request: Request) -> HTMLResponse:
    snaps = await _instance_snapshots_for(ArrKind.radarr)
    return templates.TemplateResponse(
        request, "settings_radarr.html",
        _ctx("settings/radarr", instances=snaps, kind="radarr", default_port=7878),
    )


def _normalize_base_path(p: str) -> str:
    p = (p or "/").strip()
    if not p.startswith("/"):
        p = "/" + p
    if p != "/":
        p = p.rstrip("/")
    return p


def _instance_tab(kind: ArrKind | str) -> str:
    """Maps an instance kind to its settings tab path."""
    k = kind.value if isinstance(kind, ArrKind) else kind
    return f"/settings/{k}"


@router.post("/settings/instances")
async def add_instance(
    kind: str = Form(...),
    name: str = Form(...),
    address: str = Form(...),
    port: int = Form(...),
    api_key: str = Form(...),
    base_path: str = Form("/"),
    use_ssl: str = Form("off"),
    http_timeout: int = Form(60),
) -> RedirectResponse:
    if kind not in ("sonarr", "radarr"):
        raise HTTPException(400, "kind must be sonarr or radarr")
    with session_scope() as s:
        s.add(ArrInstance(
            kind=ArrKind(kind),
            name=name.strip(),
            address=address.strip(),
            port=port,
            base_path=_normalize_base_path(base_path),
            use_ssl=(use_ssl == "on"),
            http_timeout=http_timeout,
            api_key=api_key.strip(),
        ))
    return RedirectResponse(_instance_tab(kind), status_code=303)


@router.post("/settings/instances/{instance_id}/edit")
async def edit_instance(
    instance_id: int,
    name: str = Form(...),
    address: str = Form(...),
    port: int = Form(...),
    base_path: str = Form("/"),
    use_ssl: str = Form("off"),
    http_timeout: int = Form(60),
    api_key: str = Form(""),
    enabled: str = Form("off"),
) -> RedirectResponse:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None:
            raise HTTPException(404)
        inst.name = name.strip()
        inst.address = address.strip()
        inst.port = port
        inst.base_path = _normalize_base_path(base_path)
        inst.use_ssl = (use_ssl == "on")
        inst.http_timeout = http_timeout
        inst.enabled = (enabled == "on")
        new_key = api_key.strip()
        if new_key:
            inst.api_key = new_key
        kind = inst.kind
    return RedirectResponse(_instance_tab(kind), status_code=303)


@router.post("/settings/instances/{instance_id}/delete")
async def delete_instance(instance_id: int) -> RedirectResponse:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        target = _instance_tab(inst.kind) if inst else "/settings/general"
        if inst:
            s.delete(inst)
    return RedirectResponse(target, status_code=303)


@router.post("/settings/instances/{instance_id}/mappings")
async def add_mapping(
    instance_id: int,
    remote_path: str = Form(...),
    local_path: str = Form(...),
) -> RedirectResponse:
    remote_path = remote_path.strip()
    local_path = local_path.strip()
    if not remote_path or not local_path:
        raise HTTPException(400, "remote_path and local_path required")
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None:
            raise HTTPException(404)
        s.add(PathMapping(arr_instance_id=instance_id, remote_path=remote_path, local_path=local_path))
        target = _instance_tab(inst.kind)
    return RedirectResponse(target, status_code=303)


@router.post("/settings/mappings/{mapping_id}/delete")
async def delete_mapping(mapping_id: int) -> RedirectResponse:
    with session_scope() as s:
        m = s.get(PathMapping, mapping_id)
        target = "/settings/general"
        if m:
            inst = s.get(ArrInstance, m.arr_instance_id)
            if inst:
                target = _instance_tab(inst.kind)
            s.delete(m)
    return RedirectResponse(target, status_code=303)


@router.post("/settings/general/server")
async def save_general_server(bind_address: str = Form(...)) -> RedirectResponse:
    """Persist the new bind address and re-exec the process so it takes effect."""
    from . import runtime_settings as rs
    from ..main import schedule_restart

    bind_address = bind_address.strip()
    # Cheap validation: host:port shape
    if ":" not in bind_address:
        raise HTTPException(400, "bind_address must be host:port")
    rs.set("bind_address", bind_address)
    if schedule_restart():
        return RedirectResponse("/settings/general?restarting=1", status_code=303)
    return RedirectResponse("/settings/general?manual_restart=1", status_code=303)


@router.post("/settings/general/auth")
async def save_general_auth(
    auth_method: str = Form("none"),
    username: str = Form(""),
    password: str = Form(""),
) -> RedirectResponse:
    from . import runtime_settings as rs
    from .auth import VALID_AUTH_METHODS, hash_password

    if auth_method not in VALID_AUTH_METHODS:
        raise HTTPException(400, "invalid auth_method")
    rs.set("auth_method", auth_method)

    username = username.strip()
    if username:
        rs.set("auth_username", username)
    password = password.strip()
    if password:
        if len(password) < 6:
            raise HTTPException(400, "password must be at least 6 characters")
        rs.set("auth_password_hash", hash_password(password))
    return RedirectResponse("/settings/general", status_code=303)


@router.post("/settings/general/api-key/regenerate")
async def regenerate_api_key_route() -> dict:
    from .auth import regenerate_api_key
    new_key = regenerate_api_key()
    return {"ok": True, "api_key": new_key}


@router.post("/settings/general/conversion")
async def save_general_conversion(
    delete_originals: str = Form("off"),
    max_concurrent_jobs: int = Form(1),
) -> RedirectResponse:
    from . import runtime_settings as rs
    rs.set("delete_originals", delete_originals == "on")
    # Worker reads this fresh each poll tick, so changes apply within the
    # next worker_poll_seconds without a server restart. Clamp aggressively
    # so a typo can't spawn 9000 ffmpeg processes.
    n = max(1, min(16, int(max_concurrent_jobs)))
    rs.set("max_concurrent_jobs", n)
    return RedirectResponse("/settings/general", status_code=303)


@router.post("/settings/general/encoder")
async def save_general_encoder(encoder_choice: str = Form("auto")) -> RedirectResponse:
    from . import runtime_settings as rs
    valid = {"auto"} | {p.name for p in list_known()}
    if encoder_choice not in valid:
        raise HTTPException(400, f"unknown encoder: {encoder_choice}")
    rs.set("encoder_choice", encoder_choice)
    return RedirectResponse("/settings/general", status_code=303)


@router.post("/settings/instances/{instance_id}/test")
async def test_instance(instance_id: int) -> dict:
    with session_scope() as s:
        inst = s.get(ArrInstance, instance_id)
        if inst is None:
            raise HTTPException(404, "instance not found")
        kind = inst.kind
        base, key = inst.base_url, inst.api_key
    client: SonarrClient | RadarrClient = (
        SonarrClient(base, key) if kind == ArrKind.sonarr else RadarrClient(base, key)
    )
    try:
        status = await client.system_status()
        return {"ok": True, "version": status.get("version"), "appName": status.get("appName")}
    except Exception as e:
        return {"ok": False, "error": repr(e)}
