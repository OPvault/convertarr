from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from .auth import require_auth
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..arr.radarr import RadarrClient
from ..arr.sonarr import SonarrClient
from ..db import session_scope
from ..encode.hwdetect import detect_best, is_detected, list_known
from ..models import ArrInstance, ArrKind, EntityIndex, ImageCache, Job, JobState, MediaFile, Node, PathMapping, SavedFilter, Workflow
from ..probe.codec_labels import format_conversion as _format_conversion
from ..workflows import WORKFLOW_FIELDS, WORKFLOW_OPS, VIDEO_CODEC_TARGETS, AUDIO_CODEC_TARGETS
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


def _load_entity_index(instance_id: int, kind: ArrKind) -> dict[int, dict[str, set[str]]]:
    """Read every EntityIndex row for one *arr instance, keyed by entity id.
    The indexer worker keeps this warm so filter requests don't have to do the
    N+1 fetch live."""
    with session_scope() as s:
        rows = s.execute(
            select(EntityIndex.arr_entity_id, EntityIndex.formats, EntityIndex.video_codecs)
            .where(
                EntityIndex.arr_instance_id == instance_id,
                EntityIndex.arr_kind == kind,
            )
        ).all()
    out: dict[int, dict[str, set[str]]] = {}
    for entity_id, formats, codecs in rows:
        out[entity_id] = {
            "formats": set(formats or []),
            "codecs": set(codecs or []),
        }
    return out


# Page-level cache for the *arr library responses. Series/Movies pages hit
# `list_series`/`list_movies` on every render — for big libraries that's the
# bulk of the wall-clock when navigating between pages. A short TTL keeps
# back-and-forth navigation snappy without serving meaningfully stale data
# (rescan/queue/history pages don't read this cache).
_ARR_LIBRARY_TTL = 30.0
_arr_library_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}


async def _cached_list_series(instance_id: int, base: str, key: str) -> list[dict]:
    now = asyncio.get_running_loop().time()
    cached = _arr_library_cache.get(("sonarr", instance_id))
    if cached and now - cached[0] < _ARR_LIBRARY_TTL:
        return cached[1]
    series = await SonarrClient(base, key).list_series()
    _arr_library_cache[("sonarr", instance_id)] = (now, series)
    return series


async def _cached_list_movies(instance_id: int, base: str, key: str) -> list[dict]:
    now = asyncio.get_running_loop().time()
    cached = _arr_library_cache.get(("radarr", instance_id))
    if cached and now - cached[0] < _ARR_LIBRARY_TTL:
        return cached[1]
    movies = await RadarrClient(base, key).list_movies()
    _arr_library_cache[("radarr", instance_id)] = (now, movies)
    return movies


def _enrich_movie_data(movies: list[dict], instance_id: int) -> None:
    """Attach `_formats` and `_video_codecs` to each movie in-place. Merges
    three sources, in order of accuracy:

      1. EntityIndex — pre-computed by the background indexer, covers
         everything Radarr knows about. Fast read.
      2. MediaFile probe — accurate ffprobe output for files Convertarr has
         scanned itself.
      3. Radarr `movieFile.mediaInfo` — falls back to the *arr's own metadata
         for brand-new entries the indexer hasn't visited yet."""
    from .filters import _extract_extension, video_codecs_from_probe, codecs_from_arr_mediainfo
    if not movies:
        return

    index_buckets = _load_entity_index(instance_id, ArrKind.radarr)

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
        idx = index_buckets.get(mid) or {"formats": set(), "codecs": set()}
        bucket = by_id.get(mid)
        mf = m.get("movieFile") or {}
        media_info = mf.get("mediaInfo") or {}

        formats: set[str] = set(idx["formats"])
        codecs: set[str] = set(idx["codecs"])
        if bucket:
            formats |= bucket["formats"]
            codecs |= bucket["codecs"]

        # Always merge Radarr's reported codec — handy when index + MediaFile
        # are both empty (brand-new movie); harmless when it agrees.
        for c in codecs_from_arr_mediainfo(media_info.get("videoCodec")):
            codecs.add(c)
        if not formats:
            ext = _extract_extension(mf.get("path", ""))
            if ext:
                formats.add(ext)

        m["_formats"] = sorted(formats)
        m["_video_codecs"] = sorted(codecs)


_LIST_VIEW_COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # 1 year


def _list_view_cookie(scope: str, key: str) -> str:
    return f"convertarr_{scope}_{key}"


def _resolve_list_view_prefs(
    request: Request, scope: str, filter_param: str | None,
    sort_param: str | None, dir_param: str | None,
) -> tuple[str, str, str]:
    """Combine query params with the per-scope cookie so the user's last
    selection survives a navigation away and back. Query params win when
    present (they're how the user actually picks); cookies fill the gaps.
    Returns (filter, sort, dir) — all strings, ready to use as-is."""
    cookies = request.cookies
    selected_filter = filter_param or cookies.get(_list_view_cookie(scope, "filter")) or "all"
    if filter_param is None and parse_filter_param(selected_filter) is None:
        # Cookie pointed at a custom filter that's since been deleted, or some
        # other now-invalid value — drop back to "all" instead of 500ing.
        selected_filter = "all"

    selected_sort = sort_param or cookies.get(_list_view_cookie(scope, "sort")) or DEFAULT_SORT[scope]
    raw_dir = dir_param or cookies.get(_list_view_cookie(scope, "dir"))
    selected_dir = raw_dir if raw_dir in ("asc", "desc") else DEFAULT_DIR
    return selected_filter, selected_sort, selected_dir


def _persist_list_view_prefs(
    response: Response, scope: str,
    selected_filter: str, selected_sort: str, selected_dir: str,
) -> None:
    response.set_cookie(_list_view_cookie(scope, "filter"), selected_filter,
                        max_age=_LIST_VIEW_COOKIE_MAX_AGE, samesite="lax")
    response.set_cookie(_list_view_cookie(scope, "sort"), selected_sort,
                        max_age=_LIST_VIEW_COOKIE_MAX_AGE, samesite="lax")
    response.set_cookie(_list_view_cookie(scope, "dir"), selected_dir,
                        max_age=_LIST_VIEW_COOKIE_MAX_AGE, samesite="lax")


def _enrich_series_data(series: list[dict], instance_id: int) -> None:
    """Attach `_formats` and `_video_codecs` to each series in-place.

    Reads pre-computed data from EntityIndex (kept warm by the indexer worker)
    and merges the path-prefix MediaFile join on top so freshly-probed files
    show up before the next index pass. Synchronous and DB-only — the live
    Sonarr N+1 that used to back the codec/format filters is now the indexer's
    job, so the request path is fast even for big libraries."""
    from .filters import _extract_extension, video_codecs_from_probe
    if not series:
        return

    index_buckets = _load_entity_index(instance_id, ArrKind.sonarr)

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
    mf_buckets: dict[int, dict[str, set[str]]] = {}
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
        mf_buckets[id(sr)] = bucket

    for sr in series:
        sid = sr.get("id")
        idx = index_buckets.get(sid) or {"formats": set(), "codecs": set()}
        mf = mf_buckets.get(id(sr), {"formats": set(), "codecs": set()})
        sr["_formats"] = sorted(idx["formats"] | mf["formats"])
        sr["_video_codecs"] = sorted(idx["codecs"] | mf["codecs"])


def _poster_url(images: list | None) -> str | None:
    """Pick a poster URL from a Sonarr/Radarr `images` array, prefer `remoteUrl`."""
    if not images:
        return None
    posters = [i for i in images if i.get("coverType") == "poster"]
    if not posters:
        posters = images
    poster = posters[0]
    return poster.get("remoteUrl") or poster.get("url")


def _format_relative(when: datetime | None, now: datetime | None = None) -> str:
    """Compact "time ago" — '34s', '12m', '3h', '2d'. Used in the dashboard's
    recent-activity list where absolute timestamps would be visual noise."""
    if when is None:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    secs = int((now - when).total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _format_minutes(seconds: float | None) -> str:
    """Human-friendly duration ('45s' / '2m 30s' / '1h 12m'). Mirrors the
    Statistics-page helper but kept local — the dashboard is the only other
    consumer."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _episode_label(mf: MediaFile | None) -> str:
    """`Show - S01E03` for Sonarr episodes that have season+episode populated;
    bare series name otherwise. Movies (Radarr) always render as the title."""
    if mf is None:
        return ""
    title = mf.arr_entity_title or ""
    if (
        mf.arr_kind == ArrKind.sonarr
        and mf.season_number is not None
        and mf.episode_number is not None
    ):
        return f"{title} - S{mf.season_number:02d}E{mf.episode_number:02d}"
    return title


def _dashboard_running_context() -> dict:
    """Live snapshot — polled every 2s by the dashboard hero fragment.

    Two shapes share the same payload:
      - **host mode** (default): multi-node cluster view — per-node cards
        with their in-flight jobs, plus the head of the cluster queue.
      - **worker mode** (`paired_host_url` set): this instance is a worker
        paired to another host. The "cluster" abstraction is meaningless
        from this side — there's only one node (us). The template hides
        the cluster framing and just lists the jobs we're running for the
        host. The flat `running` list is what the worker UI consumes.
    """
    from . import runtime_settings as rs

    paired_host_url = rs.get("paired_host_url", None)
    is_worker = bool(paired_host_url)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        all_nodes = s.scalars(
            select(Node).order_by(Node.is_local.desc(), Node.name)
        ).all()

        running = s.scalars(
            select(Job).where(Job.state.in_([JobState.running, JobState.cancelling]))
            .order_by(Job.started_at)
        ).all()

        # Pull the head of the queue (cap at 25 — enough to scan, not so many
        # that the polled payload bloats).
        queued = s.scalars(
            select(Job).where(Job.state == JobState.queued)
            .order_by(Job.created_at).limit(25)
        ).all()
        queued_count = s.scalar(
            select(func.count(Job.id)).where(Job.state == JobState.queued)
        ) or 0

        # ---- Per-running-job rows, grouped by owning node ----
        running_by_node: dict[str, list[dict]] = {}
        for j in running:
            mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
            title = (
                _episode_label(mf)
                or j.display_title
                or f"Job {j.id}"
            )
            # Prefer the worker-reported source_path (set on /start) so jobs
            # running on a remote worker show the path that worker actually
            # opened — host and worker may have different mount layouts.
            path = j.source_path or (mf.path if mf else "") or ""
            row = {
                "id": j.id,
                "state": j.state.value,
                "title": title,
                "path": path,
                "progress": round(j.progress_pct, 1),
                "speed": j.progress_speed,
                "fps": j.progress_fps,
                "encoder": j.encoder,
                "eta": _format_eta(_eta_seconds(j.started_at, j.progress_pct)),
                # Mirror rows on a worker carry the host's Job.id here so the
                # UI can label them "for host" and link the operator back to
                # the originating job.
                "host_job_id": j.host_job_id,
                # "AV1 → HEVC (H.265)" / "FLAC → AAC" — None when the source
                # already matches the target (no real conversion to label),
                # in which case the template skips the chip entirely.
                "video_label": _format_conversion(j.source_video_codec, j.target_video_codec),
                "audio_label": _format_conversion(j.source_audio_codec, j.target_audio_codec),
            }
            running_by_node.setdefault(j.node_id or "_unassigned", []).append(row)

        # ---- Per-node summary cards ----
        nodes_view: list[dict] = []
        for n in all_nodes:
            jobs = running_by_node.get(n.id, [])
            online = _node_online(n, now)
            nodes_view.append({
                "id": n.id,
                "name": n.name,
                "is_local": bool(n.is_local),
                "encoder_name": n.encoder_name,
                "encoder_family": n.encoder_family,
                "address": n.address,
                "max_jobs": n.max_concurrent_jobs,
                "used_slots": len(jobs),
                "online": online,
                "last_heartbeat_relative": _format_relative_short(n.last_heartbeat, now),
                "running_jobs": jobs,
            })

        node_names = {n["id"]: n["name"] for n in nodes_view}

        # ---- Cluster queue rows (with assignment + source codec) ----
        queued_rows: list[dict] = []
        for j in queued:
            mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
            source_codec = ""
            if mf and mf.probe_json:
                for st in (mf.probe_json.get("streams") or []):
                    if st.get("codec_type") != "video":
                        continue
                    if (st.get("disposition") or {}).get("attached_pic"):
                        continue
                    source_codec = (st.get("codec_name") or "").lower()
                    break
            size_gb = None
            if mf and mf.size_bytes:
                size_gb = round(mf.size_bytes / (1024 ** 3), 2)
            queued_rows.append({
                "id": j.id,
                "title": _episode_label(mf) or f"Job {j.id}",
                "path": j.source_path or (mf.path if mf else "") or "",
                "size_gb": size_gb,
                "source_codec": source_codec,
                "assignment": (
                    {"label": node_names.get(j.node_id) or "claimed", "kind": "claimed"}
                    if j.node_id else {"label": "auto", "kind": "auto"}
                ),
            })

    online_nodes = sum(1 for n in nodes_view if n["online"])
    total_slots = sum(n["max_jobs"] for n in nodes_view)
    used_slots = sum(n["used_slots"] for n in nodes_view)

    flat_running = [r for jobs in running_by_node.values() for r in jobs]
    return {
        # Mode flag — drives template branching. Worker mode hides the
        # cluster framing (it's confusing on a single-node worker view).
        "is_worker": is_worker,
        "paired_host_url": paired_host_url,
        # Top-level cluster summary (host mode)
        "cluster": {
            "total_nodes": len(nodes_view),
            "online_nodes": online_nodes,
            "active_jobs": len(running),
            "queued": queued_count,
            "total_slots": total_slots,
            "used_slots": used_slots,
        },
        # Per-node cards (each carries its own running_jobs[])
        "nodes": nodes_view,
        # Queue head (paginated to first 25)
        "queued_jobs": queued_rows,
        # Flat list of in-flight jobs, used by the worker-mode template
        # (where there's only ever one node, so per-node grouping is noise).
        "running": flat_running,
        "queued_count": queued_count,
    }


def _dashboard_context() -> dict:
    """Full dashboard snapshot — live + static. The polled fragment uses only
    the live half (`_dashboard_running_context`); the static half (24-hour
    pulse, recent activity, setup health) is rendered once per page load."""
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(hours=24)

    live = _dashboard_running_context()

    with session_scope() as s:
        # 24-hour pulse: how busy is Convertarr right now?
        done_24h = s.scalars(
            select(Job).where(
                Job.state == JobState.done,
                Job.finished_at >= yesterday,
            )
        ).all()
        failed_24h = s.scalar(
            select(func.count(Job.id)).where(
                Job.state == JobState.failed,
                Job.finished_at >= yesterday,
            )
        ) or 0
        durations = [
            (j.finished_at - j.started_at).total_seconds()
            for j in done_24h if j.started_at and j.finished_at
        ]
        avg_24h = sum(durations) / len(durations) if durations else None

        # Recent activity — last 8 finished jobs (any state). Drives the
        # dashboard's recent-activity panel.
        recent = s.scalars(
            select(Job)
            .where(Job.state.in_([JobState.done, JobState.failed, JobState.skipped]))
            .order_by(Job.finished_at.desc().nullslast())
            .limit(8)
        ).all()
        recent_rows: list[dict] = []
        for j in recent:
            mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
            duration = None
            if j.started_at and j.finished_at:
                duration = (j.finished_at - j.started_at).total_seconds()
            recent_rows.append({
                "id": j.id,
                "state": j.state.value,
                "title": (mf.arr_entity_title if mf else "") or f"Job {j.id}",
                "kind": (mf.arr_kind.value if mf and mf.arr_kind else None),
                "duration_str": _format_minutes(duration),
                "ago_str": _format_relative(j.finished_at, now),
                "encoder": j.encoder,
            })

        # Setup health snapshot
        sonarr_count = s.scalar(
            select(func.count(ArrInstance.id)).where(
                ArrInstance.kind == ArrKind.sonarr, ArrInstance.enabled.is_(True),
            )
        ) or 0
        radarr_count = s.scalar(
            select(func.count(ArrInstance.id)).where(
                ArrInstance.kind == ArrKind.radarr, ArrInstance.enabled.is_(True),
            )
        ) or 0
        workflow_count = s.scalar(
            select(func.count(Workflow.id)).where(Workflow.enabled.is_(True))
        ) or 0

    return {
        **live,
        "done_24h": len(done_24h),
        "failed_24h": failed_24h,
        "avg_24h_str": _format_minutes(avg_24h),
        "recent": recent_rows,
        "sonarr_count": sonarr_count,
        "radarr_count": radarr_count,
        "workflow_count": workflow_count,
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "dashboard.html", _ctx("dashboard", **_dashboard_context())
    )


@router.get("/dashboard/running", response_class=HTMLResponse)
async def dashboard_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_dashboard_running.html", _ctx("dashboard", **_dashboard_running_context())
    )


@router.get("/sonarr", response_class=HTMLResponse)
async def series_page(
    request: Request,
    filter: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
) -> HTMLResponse:
    selected_filter, selected_sort, selected_dir = _resolve_list_view_prefs(
        request, "series", filter, sort, dir,
    )
    spec = _resolve_filter(parse_filter_param(selected_filter))

    with session_scope() as s:
        instances = s.scalars(
            select(ArrInstance).where(ArrInstance.kind == ArrKind.sonarr, ArrInstance.enabled.is_(True))
        ).all()
        instance_snapshots = [(i.id, i.name, i.base_url, i.api_key) for i in instances]

    fetched = await asyncio.gather(
        *(_cached_list_series(inst_id, base, key) for inst_id, _, base, key in instance_snapshots),
        return_exceptions=True,
    )

    groups: list[dict] = []
    for (inst_id, name, _, _), result in zip(instance_snapshots, fetched):
        if isinstance(result, Exception):
            series, error = [], repr(result)
        else:
            series, error = result, None
        # Hide series with no downloaded episodes — there's nothing for
        # Convertarr to act on.
        series = [sr for sr in series if (sr.get("statistics") or {}).get("episodeFileCount", 0) > 0]
        # Codec/format data comes from EntityIndex (kept warm by the indexer
        # worker) plus any MediaFile probes — both DB reads, no live N+1.
        _enrich_series_data(series, inst_id)
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
    response = templates.TemplateResponse(
        request,
        "series_list.html",
        _ctx("series",
             groups=groups,
             builtin_filters=builtin, custom_filters=custom, selected_filter=selected_filter,
             sort_options=options_for("series"), selected_sort=selected_sort, selected_dir=selected_dir,
             selected_sort_label=label_for("series", selected_sort),
             workflows=_enabled_workflows_summary(),
             scope="series"),
    )
    _persist_list_view_prefs(response, "series", selected_filter, selected_sort, selected_dir)
    return response


def _assert_not_in_worker_mode() -> None:
    """If this Convertarr is currently paired AS a worker for another host,
    refuse the rescan with a structured 409. Queueing locally would just
    leave the job sitting in this install's DB forever — the local worker
    loop is paused while in worker mode (the supervisor runs the remote
    loop instead). The error body carries the host's URL so the UI can
    surface a clickable "open the host's UI" toast.
    """
    from . import runtime_settings as rs
    paired_url = rs.get("paired_host_url", None)
    if paired_url:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worker_mode",
                "host_url": paired_url,
                "message": (
                    f"This Convertarr is paired as a worker for {paired_url}. "
                    "Open that host's UI to queue rescans — they'll be picked "
                    "up by this worker automatically."
                ),
            },
        )


@router.post("/sonarr/{instance_id}/{series_id}/rescan")
async def rescan_series(instance_id: int, series_id: int,
                        workflow_id: int | None = None) -> dict:
    _assert_not_in_worker_mode()
    return await rescan_sonarr_series(instance_id, series_id, workflow_id=workflow_id)


@router.post("/sonarr/bulk-rescan")
async def bulk_rescan_series(payload: dict) -> dict:
    """Fan out per-series rescans for the bulk-select UI. The optional
    `workflow_id` in the payload pins every rescan to a specific workflow;
    without it each file is matched against all workflows."""
    _assert_not_in_worker_mode()
    items = payload.get("items") or []
    workflow_id = payload.get("workflow_id")
    workflow_id = int(workflow_id) if workflow_id else None
    if not items:
        return {"queued": 0, "skipped": 0, "failed": 0, "items": 0}
    results = await asyncio.gather(
        *(
            rescan_sonarr_series(int(i["instance_id"]), int(i["entity_id"]),
                                 workflow_id=workflow_id)
            for i in items
        ),
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


@router.get("/sonarr/{instance_id}/{series_id}", response_class=HTMLResponse)
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
    monitored_by_season = {
        sm.get("seasonNumber"): bool(sm.get("monitored", True))
        for sm in (series.get("seasons") or [])
    }

    seasons: dict[int, list[dict]] = {}
    for ep in episodes:
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

    # Hide unmonitored seasons and any monitored season that has no files
    # downloaded yet — Convertarr can't act on either, so they're noise here.
    # Within each shown season, latest episode first.
    visible_seasons = []
    for sn, eps in seasons.items():
        if not monitored_by_season.get(sn, True):
            continue
        if not any(e["has_file"] for e in eps):
            continue
        eps.sort(key=lambda e: -(e["number"] or 0))
        visible_seasons.append((sn, eps))
    # Highest season first; Specials (0) sinks to the bottom.
    visible_seasons.sort(key=lambda kv: (1, 0) if kv[0] == 0 else (0, -kv[0]))

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
        "seasons": visible_seasons,
    }

    return templates.TemplateResponse(
        request, "series_detail.html",
        _ctx("series", series=detail, workflows=_enabled_workflows_summary()),
    )


@router.post("/sonarr/{instance_id}/episodefile/{episode_file_id}/rescan")
async def rescan_episode_file(instance_id: int, episode_file_id: int,
                              workflow_id: int | None = None) -> dict:
    _assert_not_in_worker_mode()
    return await rescan_sonarr_episode_file(instance_id, episode_file_id,
                                            workflow_id=workflow_id)


@router.post("/sonarr/{instance_id}/{series_id}/season/{season_number}/rescan")
async def rescan_season(instance_id: int, series_id: int, season_number: int,
                        workflow_id: int | None = None) -> dict:
    _assert_not_in_worker_mode()
    return await rescan_sonarr_season(instance_id, series_id, season_number,
                                      workflow_id=workflow_id)


@router.get("/radarr", response_class=HTMLResponse)
async def movies_page(
    request: Request,
    filter: str | None = None,
    sort: str | None = None,
    dir: str | None = None,
) -> HTMLResponse:
    selected_filter, selected_sort, selected_dir = _resolve_list_view_prefs(
        request, "movies", filter, sort, dir,
    )
    spec = _resolve_filter(parse_filter_param(selected_filter))

    with session_scope() as s:
        instances = s.scalars(
            select(ArrInstance).where(ArrInstance.kind == ArrKind.radarr, ArrInstance.enabled.is_(True))
        ).all()
        instance_snapshots = [(i.id, i.name, i.base_url, i.api_key) for i in instances]

    fetched = await asyncio.gather(
        *(_cached_list_movies(inst_id, base, key) for inst_id, _, base, key in instance_snapshots),
        return_exceptions=True,
    )

    groups: list[dict] = []
    for (inst_id, name, _, _), result in zip(instance_snapshots, fetched):
        if isinstance(result, Exception):
            movies, error = [], repr(result)
        else:
            movies, error = result, None
        # Hide movies without a downloaded file — Convertarr can't act on them.
        movies = [m for m in movies if m.get("hasFile")]
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
    response = templates.TemplateResponse(
        request,
        "movies_list.html",
        _ctx("movies",
             groups=groups,
             builtin_filters=builtin, custom_filters=custom, selected_filter=selected_filter,
             sort_options=options_for("movies"), selected_sort=selected_sort, selected_dir=selected_dir,
             selected_sort_label=label_for("movies", selected_sort),
             workflows=_enabled_workflows_summary(),
             scope="movies"),
    )
    _persist_list_view_prefs(response, "movies", selected_filter, selected_sort, selected_dir)
    return response


@router.post("/radarr/{instance_id}/{movie_id}/rescan")
async def rescan_movie(instance_id: int, movie_id: int,
                       workflow_id: int | None = None) -> dict:
    _assert_not_in_worker_mode()
    return await rescan_radarr_movie(instance_id, movie_id, workflow_id=workflow_id)


@router.post("/radarr/bulk-rescan")
async def bulk_rescan_movies(payload: dict) -> dict:
    """Fan out per-movie rescans for the bulk-select UI."""
    _assert_not_in_worker_mode()
    items = payload.get("items") or []
    workflow_id = payload.get("workflow_id")
    workflow_id = int(workflow_id) if workflow_id else None
    if not items:
        return {"queued": 0, "skipped": 0, "failed": 0, "items": 0}
    results = await asyncio.gather(
        *(
            rescan_radarr_movie(int(i["instance_id"]), int(i["entity_id"]),
                                workflow_id=workflow_id)
            for i in items
        ),
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


@router.get("/radarr/{instance_id}/{movie_id}", response_class=HTMLResponse)
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
        request, "movie_detail.html",
        _ctx("movies", movie=detail, workflows=_enabled_workflows_summary()),
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


@router.get("/api/search")
async def global_search(q: str = "", limit: int = 8, kind: str = "both") -> dict:
    """Top-bar autocomplete — substring/prefix match across enabled
    Sonarr + Radarr instances. `kind` narrows the search to one side
    (used by the Sonarr/Radarr library pages); default queries both.
    Backed by the shared `_cached_list_*` so repeated keystrokes don't
    refetch from the *arrs."""
    query = q.strip().lower()
    if len(query) < 2:
        return {"results": []}

    kind_filter: ArrKind | None
    if kind == "sonarr":
        kind_filter = ArrKind.sonarr
    elif kind == "radarr":
        kind_filter = ArrKind.radarr
    else:
        kind_filter = None

    with session_scope() as s:
        stmt = select(ArrInstance).where(ArrInstance.enabled.is_(True))
        if kind_filter is not None:
            stmt = stmt.where(ArrInstance.kind == kind_filter)
        instances = s.scalars(stmt).all()
        snapshots = [(i.id, i.kind, i.base_url, i.api_key) for i in instances]

    async def _fetch(inst_id: int, kind: ArrKind, base: str, key: str):
        try:
            if kind == ArrKind.sonarr:
                return kind, inst_id, await _cached_list_series(inst_id, base, key)
            return kind, inst_id, await _cached_list_movies(inst_id, base, key)
        except Exception:
            return kind, inst_id, []

    fetched = await asyncio.gather(*(_fetch(*sn) for sn in snapshots))

    def _score(title: str) -> int | None:
        t = title.lower()
        if not t:
            return None
        if t.startswith(query):
            return 0
        if any(part.startswith(query) for part in t.split()):
            return 1
        if query in t:
            return 2
        return None

    scored: list[tuple[int, int, dict]] = []  # (score, year_neg, payload)
    for kind, inst_id, items in fetched:
        for it in items:
            title = it.get("title", "") or ""
            # Series-only: episode files. Movies: hasFile. Match list-page rules.
            if kind == ArrKind.sonarr:
                if (it.get("statistics") or {}).get("episodeFileCount", 0) <= 0:
                    continue
                alt_titles = [a.get("title", "") for a in (it.get("alternateTitles") or [])]
            else:
                if not it.get("hasFile"):
                    continue
                alt_titles = [it.get("originalTitle", "") or ""]

            best = None
            matched_alt = None
            for cand in [title, *alt_titles]:
                s_ = _score(cand)
                if s_ is None:
                    continue
                if best is None or s_ < best:
                    best = s_
                    matched_alt = cand if cand != title else None
            if best is None:
                continue

            year = it.get("year") or 0
            kind_label = "sonarr" if kind == ArrKind.sonarr else "radarr"
            poster = _poster_url(it.get("images"))
            scored.append((
                best, -year,
                {
                    "kind": kind_label,
                    "instance_id": inst_id,
                    "id": it.get("id"),
                    "title": title,
                    "alt_title": matched_alt if matched_alt and matched_alt != title else None,
                    "year": it.get("year"),
                    "url": f"/{kind_label}/{inst_id}/{it.get('id')}",
                    "poster_proxy": f"/img/{kind_label}/{inst_id}?u={poster}" if poster else None,
                },
            ))

    scored.sort(key=lambda x: (x[0], x[1], x[2]["title"].lower()))
    cap = max(1, min(limit, 20))
    return {"results": [p for _, _, p in scored[:cap]]}


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


def _clean_filter_payload(payload: dict) -> tuple[str, str, list[dict]]:
    scope = payload.get("scope")
    name = (payload.get("name") or "").strip()
    clauses = payload.get("clauses") or []
    if scope not in ("series", "movies") or not name:
        raise HTTPException(400, "scope + non-empty name required")
    if not isinstance(clauses, list):
        raise HTTPException(400, "clauses must be a list")
    cleaned: list[dict] = []
    for c in clauses:
        if not isinstance(c, dict):
            continue
        if c.get("field") not in CUSTOM_FIELDS:
            continue
        if c.get("op") not in CUSTOM_OPS:
            continue
        cleaned.append({"field": c["field"], "op": c["op"], "value": str(c.get("value", ""))})
    return scope, name, cleaned


@router.post("/api/filters")
async def create_filter(payload: dict) -> dict:
    """Body: {scope: "series"|"movies", name: str, clauses: [{field, op, value}]}"""
    scope, name, cleaned = _clean_filter_payload(payload)
    with session_scope() as s:
        f = SavedFilter(scope=scope, name=name, clauses=cleaned)
        s.add(f)
        s.flush()
        return {"id": f.id, "scope": scope, "name": name, "clauses": cleaned}


@router.post("/api/filters/{filter_id}")
async def update_filter(filter_id: int, payload: dict) -> dict:
    """Update an existing saved filter in place. Used by the editor's Save
    button when the user is editing instead of creating — without this the JS
    falls back to POST /api/filters and ends up duplicating the row."""
    scope, name, cleaned = _clean_filter_payload(payload)
    with session_scope() as s:
        f = s.get(SavedFilter, filter_id)
        if f is None:
            raise HTTPException(404, "filter not found")
        if f.scope != scope:
            raise HTTPException(400, "scope mismatch")
        f.name = name
        f.clauses = cleaned
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
            mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
            eta = _eta_seconds(j.started_at, j.progress_pct)
            rows.append({
                "id": j.id,
                "state": j.state.value,
                "progress": round(j.progress_pct, 1),
                "speed": j.progress_speed,
                "fps": j.progress_fps,
                "encoder": j.encoder,
                "title": _episode_label(mf) or j.display_title or "",
                # Worker-reported source_path (set on /start) is the path the
                # worker actually opens — for remote nodes it differs from
                # mf.path because of per-node mount layout. Falls back to
                # the host's mf.path while a job is still queued (no worker
                # has claimed it yet).
                "path": j.source_path or (mf.path if mf else "") or "",
                "reason": mf.reason if mf else "",
                "eta": _format_eta(eta),
                "host_job_id": j.host_job_id,
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


@router.post("/queue/{job_id}/force-delete")
async def force_delete_job(job_id: int) -> dict:
    """Hard-delete a job row regardless of state — for jobs stuck in
    `cancelling` because the owning worker is gone, or any other state
    where the operator just wants the row gone.

    Different from /cancel: this DROPS the row from the database. It won't
    show up in History either. Best-effort SIGTERMs the local subprocess
    first in case the ffmpeg is still alive on this machine.
    """
    from ..encode.runner import runner
    runner.cancel(job_id)
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None:
            raise HTTPException(404)
        prev_state = j.state.value
        s.delete(j)
    log = logging.getLogger(__name__)
    log.warning("job %d force-deleted from DB (was %s)", job_id, prev_state)
    return {"ok": True, "deleted": True, "previous": prev_state}


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
            mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
            # Prefer the worker-reported source_path so jobs that ran on a
            # remote node show the path that node actually saw on disk
            # (worker and host can have different mount layouts). Fall
            # back to MediaFile, then to the dispatch's display title.
            title = _episode_label(mf) or j.display_title or ""
            path = j.source_path or (mf.path if mf else "")
            rows.append({
                "id": j.id,
                "state": j.state.value,
                "encoder": j.encoder,
                "title": title,
                "path": path,
                "output_path": j.output_path,
                "error_tail": j.error_tail,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
                "node": j.node_name,
                "host_job_id": j.host_job_id,
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
    from ..workers.local_node import LOCAL_NODE_ID
    encoders = [
        {"name": p.name, "label": p.label, "family": p.family, "detected": is_detected(p)}
        for p in list_known()
    ]
    with session_scope() as s:
        local = s.get(Node, LOCAL_NODE_ID)
        local_max_concurrent = local.max_concurrent_jobs if local else 1
    return templates.TemplateResponse(
        request,
        "settings_general.html",
        _ctx(
            "settings/general",
            auth_method=rs.get("auth_method", "none"),
            auth_username=rs.get("auth_username", "admin"),
            api_key=rs.get("api_key", ""),
            delete_originals=bool(rs.get("delete_originals", True)),
            encoders=encoders,
            encoder_choice=rs.get("encoder_choice", "auto"),
            active_encoder=detect_best(),
            max_concurrent_jobs=local_max_concurrent,
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
    from ..workers.local_node import LOCAL_NODE_ID
    rs.set("delete_originals", delete_originals == "on")
    n_jobs = max(1, min(16, int(max_concurrent_jobs)))
    with session_scope() as s:
        local = s.get(Node, LOCAL_NODE_ID)
        if local is not None:
            local.max_concurrent_jobs = n_jobs
    return RedirectResponse("/settings/general", status_code=303)


@router.post("/settings/general/encoder")
async def save_general_encoder(encoder_choice: str = Form("auto")) -> RedirectResponse:
    from . import runtime_settings as rs
    valid = {"auto"} | {p.name for p in list_known()}
    if encoder_choice not in valid:
        raise HTTPException(400, f"unknown encoder: {encoder_choice}")
    rs.set("encoder_choice", encoder_choice)
    return RedirectResponse("/settings/general", status_code=303)


# ---- Workflows -----------------------------------------------------------

def _clean_workflow_payload(payload: dict) -> tuple[str, bool, int, list[dict], str, str]:
    """Validate the JSON body of a workflow create/update request. Drops
    clauses with unknown fields/ops rather than 400-ing — same lenient policy
    as the filter editor uses, so an old saved workflow doesn't break when we
    add or rename a field."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    enabled = bool(payload.get("enabled", True))
    try:
        priority = int(payload.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    raw_conditions = payload.get("conditions") or []
    if not isinstance(raw_conditions, list):
        raise HTTPException(400, "conditions must be a list")
    cleaned: list[dict] = []
    for c in raw_conditions:
        if not isinstance(c, dict):
            continue
        if c.get("field") not in WORKFLOW_FIELDS:
            continue
        if c.get("op") not in WORKFLOW_OPS:
            continue
        # `connector` joins this clause to the previous; defaults to "and".
        # Anything other than "or" gets coerced back to "and" so we never
        # store free-form strings the matcher would have to defend against.
        connector = "or" if str(c.get("connector") or "and").lower() == "or" else "and"

        # Value is always persisted as a list of strings. The editor sends a
        # list for string-typed fields (multi-select chips) and a single
        # entry for numbers; legacy single-string values are wrapped here
        # so old data round-trips identically.
        raw_value = c.get("value", "")
        if isinstance(raw_value, list):
            value = [str(v).strip() for v in raw_value if str(v).strip()]
        else:
            s = str(raw_value).strip()
            value = [s] if s else []

        cleaned.append({
            "field": c["field"], "op": c["op"], "value": value,
            "connector": connector,
        })

    target_video = (payload.get("target_video_codec") or "hevc").lower()
    target_audio = (payload.get("target_audio_codec") or "aac").lower()
    if target_video not in VIDEO_CODEC_TARGETS:
        raise HTTPException(400, f"target_video_codec must be one of {VIDEO_CODEC_TARGETS}")
    if target_audio not in AUDIO_CODEC_TARGETS:
        raise HTTPException(400, f"target_audio_codec must be one of {AUDIO_CODEC_TARGETS}")
    return name, enabled, priority, cleaned, target_video, target_audio


def _workflow_dict(w: Workflow) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "enabled": bool(w.enabled),
        "priority": w.priority,
        "conditions": list(w.conditions or []),
        "target_video_codec": w.target_video_codec,
        "target_audio_codec": w.target_audio_codec,
    }


_DEFAULT_NAME_PATTERN = re.compile(r"^\s*workflow\s+(\d+)\s*$", re.IGNORECASE)


def _next_workflow_default_name() -> str:
    """Suggest 'Workflow N' for a fresh editor — N = max existing match + 1.

    Scans for names already matching `Workflow <int>` (case-insensitive,
    whitespace-tolerant) so renaming "Workflow 3" to something else doesn't
    cause us to suggest "Workflow 3" again. Falls back to "Workflow 1" when
    nothing matches."""
    with session_scope() as s:
        names = s.scalars(select(Workflow.name)).all()
    used: list[int] = []
    for n in names:
        m = _DEFAULT_NAME_PATTERN.match(n or "")
        if m:
            try:
                used.append(int(m.group(1)))
            except ValueError:
                pass
    return f"Workflow {max(used, default=0) + 1}"


def _enabled_workflows_summary() -> list[dict]:
    """`{id, name, priority}` for every enabled workflow, ordered the same as
    the matcher walks them. The detail-page rescan picker uses this to render
    a dropdown when there's more than one — and to disable the rescan button
    entirely when there are zero."""
    with session_scope() as s:
        rows = s.scalars(
            select(Workflow).where(Workflow.enabled.is_(True)).order_by(Workflow.priority, Workflow.id)
        ).all()
        return [{"id": w.id, "name": w.name, "priority": w.priority} for w in rows]


def _workflow_meta() -> dict:
    """Schema the JS editor consumes — fields/ops dropdowns, target codec
    choices. Centralized so the list page and the editor page stay in sync."""
    return {
        "fields": [
            {"key": k, "label": v["label"], "type": v["type"], "suggestions": v.get("suggestions") or []}
            for k, v in WORKFLOW_FIELDS.items()
        ],
        "ops": [
            {"key": k, "label": v["label"], "applies_to": list(v["applies_to"])}
            for k, v in WORKFLOW_OPS.items()
        ],
        "video_targets": list(VIDEO_CODEC_TARGETS),
        "audio_targets": list(AUDIO_CODEC_TARGETS),
    }


@router.get("/settings/workflows", response_class=HTMLResponse)
async def settings_workflows(request: Request) -> HTMLResponse:
    with session_scope() as s:
        workflows = [
            _workflow_dict(w) for w in s.scalars(
                select(Workflow).order_by(Workflow.priority, Workflow.id)
            ).all()
        ]
    return templates.TemplateResponse(
        request, "settings_workflows.html",
        _ctx("settings/workflows", workflows=workflows, **_workflow_meta()),
    )


@router.get("/settings/workflows/edit/{wf_id}", response_class=HTMLResponse)
async def settings_workflow_edit(request: Request, wf_id: str) -> HTMLResponse:
    """Full-page node-graph editor. `wf_id` is either an integer id (edit) or
    the literal string `new` (create)."""
    if wf_id == "new":
        existing = None
    else:
        try:
            wid = int(wf_id)
        except ValueError:
            raise HTTPException(404, "workflow not found")
        with session_scope() as s:
            w = s.get(Workflow, wid)
            if w is None:
                raise HTTPException(404, "workflow not found")
            existing = _workflow_dict(w)
    return templates.TemplateResponse(
        request, "settings_workflow_edit.html",
        _ctx(
            "settings/workflows",
            workflow=existing,
            default_name=_next_workflow_default_name() if existing is None else "",
            **_workflow_meta(),
        ),
    )


@router.post("/api/workflows")
async def create_workflow(payload: dict) -> dict:
    name, enabled, priority, conditions, target_video, target_audio = _clean_workflow_payload(payload)
    with session_scope() as s:
        w = Workflow(
            name=name, enabled=enabled, priority=priority, conditions=conditions,
            target_video_codec=target_video, target_audio_codec=target_audio,
        )
        s.add(w)
        s.flush()
        return _workflow_dict(w)


@router.post("/api/workflows/{workflow_id}")
async def update_workflow(workflow_id: int, payload: dict) -> dict:
    name, enabled, priority, conditions, target_video, target_audio = _clean_workflow_payload(payload)
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        if w is None:
            raise HTTPException(404, "workflow not found")
        w.name = name
        w.enabled = enabled
        w.priority = priority
        w.conditions = conditions
        w.target_video_codec = target_video
        w.target_audio_codec = target_audio
        return _workflow_dict(w)


@router.post("/api/workflows/{workflow_id}/delete")
async def delete_workflow(workflow_id: int) -> dict:
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        if w is not None:
            s.delete(w)
    return {"ok": True}


def _workflow_export_payload(w: Workflow) -> dict:
    """The JSON shape we emit for a single-workflow export. Bare object —
    no wrapper — so the user sees a clean, paste-friendly definition."""
    return {
        "name": w.name,
        "enabled": bool(w.enabled),
        "priority": w.priority,
        "conditions": list(w.conditions or []),
        "target_video_codec": w.target_video_codec,
        "target_audio_codec": w.target_audio_codec,
    }


@router.get("/settings/workflows/{workflow_id}/export")
async def export_workflow(workflow_id: int) -> Response:
    """Download a single workflow as JSON. Per-workflow rather than
    everything-at-once — the import side only takes one at a time, so
    keeping export symmetric makes copy-and-paste between instances clean."""
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        if w is None:
            raise HTTPException(404, "workflow not found")
        payload = _workflow_export_payload(w)
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", w.name).strip("-").lower() or f"workflow-{w.id}"
    body = json.dumps(payload, indent=2).encode("utf-8")
    fname = f"convertarr-{slug}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _extract_single_workflow(payload: object) -> dict:
    """Pull one workflow out of whatever shape the user pasted. Tolerates
    bare objects, single-item arrays, the old bulk-export shape (only when
    it contains exactly one item), and a {"workflow": {...}} wrapper.
    Rejects anything containing more than one workflow — the user has to
    import them one at a time so they can review each before saving."""
    if isinstance(payload, list):
        if len(payload) != 1:
            raise HTTPException(
                400, "Paste exactly one workflow — this JSON has " + str(len(payload)),
            )
        item = payload[0]
    elif isinstance(payload, dict):
        if isinstance(payload.get("workflow"), dict):
            item = payload["workflow"]
        elif isinstance(payload.get("workflows"), list):
            wf_list = payload["workflows"]
            if len(wf_list) != 1:
                raise HTTPException(
                    400,
                    "This file has " + str(len(wf_list)) + " workflows. "
                    "Import them one at a time.",
                )
            item = wf_list[0]
        elif "name" in payload:
            # Bare workflow object — what `_workflow_export_payload` produces.
            item = payload
        else:
            raise HTTPException(400, "Couldn't find a workflow in this JSON")
    else:
        raise HTTPException(400, "Expected a JSON object")
    if not isinstance(item, dict):
        raise HTTPException(400, "Workflow must be a JSON object")
    return item


@router.post("/settings/workflows/import")
async def import_workflow(file: UploadFile = File(...)) -> RedirectResponse:
    """Additive single-workflow import. Multipart upload preserves the
    shared endpoint that both the file picker and the textarea-paste flow
    use (the JS wraps the textarea contents in a Blob)."""
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e.msg}")

    item = _extract_single_workflow(payload)

    name = (item.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Workflow needs a non-empty name")
    try:
        priority = int(item.get("priority", 100))
    except (TypeError, ValueError):
        priority = 100
    target_video = (item.get("target_video_codec") or "hevc").lower()
    target_audio = (item.get("target_audio_codec") or "aac").lower()
    if target_video not in VIDEO_CODEC_TARGETS:
        target_video = "hevc"
    if target_audio not in AUDIO_CODEC_TARGETS:
        target_audio = "aac"
    conditions = item.get("conditions") or []
    if not isinstance(conditions, list):
        conditions = []

    with session_scope() as s:
        s.add(Workflow(
            name=name,
            enabled=bool(item.get("enabled", True)),
            priority=priority,
            conditions=conditions,
            target_video_codec=target_video,
            target_audio_codec=target_audio,
        ))
    return RedirectResponse("/settings/workflows?imported=1", status_code=303)


@router.post("/api/workflows/{workflow_id}/toggle")
async def toggle_workflow(workflow_id: int) -> dict:
    """Flip the enabled flag without round-tripping the full editor — used by
    the per-row toggle on the list page."""
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        if w is None:
            raise HTTPException(404, "workflow not found")
        w.enabled = not w.enabled
        return _workflow_dict(w)


# ---- Nodes (settings page + CRUD) ----------------------------------------

def _format_relative_short(when: datetime | None, now: datetime | None = None) -> str:
    """'34s ago' / '12m ago' / '3h ago' / '—'. Used in the Nodes UI to show
    when each node last checked in."""
    if when is None:
        return "—"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    secs = int((now - when).total_seconds())
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _node_online(n: Node, now: datetime) -> bool:
    """Match the watchdog's 90s threshold so the UI dot agrees with what the
    server is about to do (revive jobs from a stale node). The local node is
    always online: if this code is rendering, the host process is alive, so
    the worker that lives in it is alive too — heartbeat lag from a busy
    encode loop or a brief supervisor mode-switch shouldn't show as offline.
    Mirrors the watchdog exemption in `workers/heartbeat.py:sweep_once`.
    """
    if n.is_local:
        return True
    from ..workers.heartbeat import STALE_THRESHOLD_SECONDS
    if n.last_heartbeat is None:
        return False
    hb = n.last_heartbeat
    if hb.tzinfo is None:
        hb = hb.replace(tzinfo=timezone.utc)
    return (now - hb).total_seconds() < STALE_THRESHOLD_SECONDS


@router.get("/settings/nodes", response_class=HTMLResponse)
async def settings_nodes(request: Request) -> HTMLResponse:
    from . import runtime_settings as rs

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        # The local node represents THIS host itself — it isn't a separate
        # machine the operator manages. Its only knob (max_concurrent_jobs)
        # lives on /settings/general; remote workers are listed below.
        node_rows = s.scalars(
            select(Node).where(Node.is_local == False).order_by(Node.name)  # noqa: E712
        ).all()
        # Per-node running counts in one go.
        running_by_node: dict[str, int] = {}
        for nid, count in s.execute(
            select(Job.node_id, func.count(Job.id))
            .where(Job.state == JobState.running)
            .group_by(Job.node_id)
        ).all():
            if nid:
                running_by_node[nid] = count or 0

        nodes_view: list[dict] = []
        for n in node_rows:
            nodes_view.append({
                "id": n.id,
                "name": n.name,
                "is_local": bool(n.is_local),
                "encoder_family": n.encoder_family,
                "encoder_name": n.encoder_name,
                "encoder_choice": n.encoder_choice or "auto",
                "max_concurrent_jobs": n.max_concurrent_jobs,
                "address": n.address,
                "version": n.version,
                "last_heartbeat_relative": _format_relative_short(n.last_heartbeat, now),
                "online": _node_online(n, now),
                "running_count": running_by_node.get(n.id, 0),
                "is_paired": bool(n.pair_url),  # True for nodes added via the pair form
            })

    known_encoders = [
        {"name": p.name, "family": p.family, "label": p.label}
        for p in list_known()
    ]
    # Is THIS instance itself acting as a worker? If so, surface a banner.
    self_paired_url = rs.get("paired_host_url", None)
    self_paired_at = rs.get("paired_at", None)
    return templates.TemplateResponse(
        request, "settings_nodes.html",
        _ctx(
            "settings/nodes",
            nodes=nodes_view,
            known_encoders=known_encoders,
            self_paired_url=self_paired_url,
            self_paired_at=self_paired_at,
            paired_query=request.query_params.get("paired"),
        ),
    )


@router.post("/settings/nodes/{node_id}/edit")
async def edit_node(
    node_id: str,
    name: str = Form(...),
    encoder_choice: str = Form("auto"),
    max_concurrent_jobs: int | None = Form(None),
) -> RedirectResponse:
    name = name.strip() or node_id[:8]
    valid_encoders = {"auto"} | {p.name for p in list_known()}
    if encoder_choice not in valid_encoders:
        encoder_choice = "auto"
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        n.name = name
        n.encoder_choice = encoder_choice
        # Only the local node's concurrency is editable from this host.
        # Remote workers own their own setting and report it via heartbeat.
        if n.is_local and max_concurrent_jobs is not None:
            n.max_concurrent_jobs = max(1, min(16, int(max_concurrent_jobs)))
    return RedirectResponse("/settings/nodes", status_code=303)


@router.post("/settings/nodes/{node_id}/delete")
async def delete_node(node_id: str) -> RedirectResponse:
    if node_id == "local":
        raise HTTPException(400, "the host's built-in worker can't be deleted")
    # Best-effort: tell the worker to forget its pairing, so its supervisor
    # flips back to host mode immediately. We don't have its api_key in our
    # records (the operator typed it once into the pair form, we never
    # persisted it) — without it we can't authenticate the disconnect call.
    # If the row was created via `/api/v1/nodes/register` rather than via
    # the pair flow, there's no pairing to clear anyway. Either way, fall
    # through to deleting the local row + reviving its in-flight jobs.
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is not None:
            for j in s.scalars(select(Job).where(Job.node_id == node_id, Job.state.in_([JobState.running, JobState.cancelling]))).all():
                j.state = JobState.queued
                j.node_id = None
                j.started_at = None
                j.progress_pct = 0.0
            s.delete(n)
    return RedirectResponse("/settings/nodes", status_code=303)


@router.post("/settings/nodes/pair")
async def pair_node(
    request: Request,
    address: str = Form(...),
    worker_api_key: str = Form(...),
    name: str = Form(""),
) -> RedirectResponse:
    """Enlist a remote Convertarr instance as a worker for this host.

    The operator types the worker's address (host:port or full URL) and
    the worker's API key. We POST `/api/v1/pairing/accept` to the worker,
    using the worker's api_key as our X-Api-Key. The body carries our own
    URL + api_key so the worker can call back. After the worker accepts,
    its supervisor switches into worker-mode and registers with us.
    """
    from . import runtime_settings as rs

    addr = (address or "").strip()
    if not addr:
        raise HTTPException(400, "address is required")
    worker_api_key = (worker_api_key or "").strip()
    if not worker_api_key:
        raise HTTPException(400, "worker API key is required")
    name = (name or "").strip()

    # Accept either "host:port" / "host" / full URL.
    if not addr.startswith(("http://", "https://")):
        addr = "http://" + addr
    addr = addr.rstrip("/")

    # Tell the worker who we are. Build our own callable URL from the request
    # context — but we don't have a Request here, so use the api_key + the
    # canonical bind address. Operator can override `host_url` later by
    # editing the worker's runtime_settings if their topology requires it.
    own_api_key = rs.get("api_key", "")
    if not own_api_key:
        raise HTTPException(500, "host has no api_key set; configure one first")

    # The URL the worker should call back on. The operator's browser is by
    # definition able to reach this host at the URL in their address bar
    # (otherwise they wouldn't have loaded the page) — so the request's Host
    # header is almost always the right answer. This matters when the host
    # is in Docker: socket-derived "primary IP" gives the bridge address
    # (e.g. 172.28.0.2), which is unreachable from a worker on the LAN.
    # Order of preference:
    #   1. CONVERTARR_HOST_URL_HINT env var (operator override).
    #   2. The request's Host header (covers the Docker case).
    #   3. socket-trick fallback for headless / no-Host scenarios.
    import os
    hint = os.environ.get("CONVERTARR_HOST_URL_HINT")
    if hint:
        own_url = hint if hint.startswith(("http://", "https://")) else f"http://{hint}"
        own_url = own_url.rstrip("/")
    elif request.url.hostname:
        port = request.url.port
        netloc = request.url.hostname + (f":{port}" if port else "")
        own_url = f"{request.url.scheme}://{netloc}"
    else:
        own_url = "http://" + (request_host_for_pairing() or "convertarr") + ":6565"

    # Send accept
    pair_url = f"{addr}/api/v1/pairing/accept"
    body = {"host_url": own_url, "host_api_key": own_api_key, "name": name}
    headers = {"X-Api-Key": worker_api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(pair_url, json=body, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"couldn't reach worker at {addr}: {e!r}")
    if r.status_code == 401:
        raise HTTPException(401, "worker rejected the API key — double-check it")
    if r.status_code >= 400:
        raise HTTPException(502, f"worker returned {r.status_code}: {r.text[:200]}")

    data = r.json() if r.content else {}
    node_id = (data.get("node_id") or "").strip()
    if not node_id:
        raise HTTPException(502, "worker returned no node_id")
    display_name = name or (data.get("name") or addr)

    # Pre-create the Node row so it shows up immediately with the
    # pair_url/api_key set — the worker's subsequent /register call will
    # refresh the encoder fields. Without this, the row would only appear
    # after the worker's supervisor switched modes (~5s) and called register.
    now = datetime.now(timezone.utc)
    with session_scope() as s:
        existing = s.get(Node, node_id)
        if existing is None:
            s.add(Node(
                id=node_id,
                name=display_name,
                is_local=False,
                max_concurrent_jobs=1,
                last_register=now,
                last_heartbeat=now,
                address=addr.replace("http://", "").replace("https://", ""),
                pair_url=addr,
                pair_api_key=worker_api_key,
            ))
        else:
            existing.name = display_name
            existing.pair_url = addr
            existing.pair_api_key = worker_api_key
            existing.address = addr.replace("http://", "").replace("https://", "")
            existing.last_register = now
    return RedirectResponse(
        f"/settings/nodes?paired={node_id}", status_code=303,
    )


def request_host_for_pairing() -> str | None:
    """Best-effort: derive a hostname/IP the worker can use to call back to
    us. Reads CONVERTARR_HOST_URL_HINT first (operator-supplied override),
    falls back to the machine's primary IP via socket lookup. Returns the
    hostname only — the port is appended by the caller.
    """
    import os
    import socket

    hint = os.environ.get("CONVERTARR_HOST_URL_HINT")
    if hint:
        # Strip scheme + port if the operator passed a full URL.
        hint = hint.replace("http://", "").replace("https://", "")
        return hint.split(":", 1)[0].rstrip("/")
    # socket trick: which interface IP would we use to talk to anything?
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


@router.post("/settings/nodes/{node_id}/unpair")
async def unpair_node(node_id: str) -> RedirectResponse:
    """Tell a paired worker to forget us (its supervisor reverts to host
    mode), then drop the local Node row. Reads the worker's address +
    api_key from `Node.pair_url` / `Node.pair_api_key`, set when the operator
    paired via the UI."""
    if node_id == "local":
        raise HTTPException(400, "the host's built-in worker can't be unpaired")
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is None:
            return RedirectResponse("/settings/nodes", status_code=303)
        pair_url = n.pair_url
        pair_api_key = n.pair_api_key
    if pair_url and pair_api_key:
        headers = {"X-Api-Key": pair_api_key, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{pair_url}/api/v1/pairing/disconnect",
                                  json={}, headers=headers)
        except httpx.HTTPError:
            # Worker is unreachable — drop the local row anyway so the UI
            # isn't stuck showing a node we can't manage.
            pass
    with session_scope() as s:
        n = s.get(Node, node_id)
        if n is not None:
            for j in s.scalars(select(Job).where(Job.node_id == node_id, Job.state.in_([JobState.running, JobState.cancelling]))).all():
                j.state = JobState.queued
                j.node_id = None
                j.started_at = None
                j.progress_pct = 0.0
            s.delete(n)
    return RedirectResponse("/settings/nodes", status_code=303)


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
