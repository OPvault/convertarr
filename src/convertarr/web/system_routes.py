"""System pages — Logs (app + per-job) and Backup (config export/import)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..config import settings
from ..db import session_scope
from ..models import ArrInstance, ArrKind, EntityIndex, Job, JobState, MediaFile, PathMapping, SavedFilter, Workflow
from ..probe.codec_labels import canonical_codec as _canonical_codec
from . import runtime_settings as rs
from .auth import require_auth

router = APIRouter(dependencies=[Depends(require_auth)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _ctx(active: str, **extra) -> dict:
    """Mirror the helper in routes.py — local copy avoids a circular import."""
    from ..encode.hwdetect import detect_best
    return {"active": active, "active_encoder_label": detect_best().label, **extra}


# ---------- Logs ----------

@router.get("/system", response_class=HTMLResponse)
async def system_root() -> RedirectResponse:
    return RedirectResponse("/system/logs", status_code=302)


@router.get("/system/logs", response_class=HTMLResponse)
async def system_logs(request: Request, tab: str = "app") -> HTMLResponse:
    if tab not in ("app", "jobs"):
        tab = "app"
    if tab == "jobs":
        with session_scope() as s:
            jobs = s.scalars(select(Job).order_by(Job.id.desc()).limit(100)).all()
            rows = []
            for j in jobs:
                mf = s.get(MediaFile, j.media_file_id) if j.media_file_id else None
                rows.append({
                    "id": j.id,
                    "state": j.state.value,
                    "title": (mf.arr_entity_title if mf else None) or j.display_title or "",
                    "log_path": j.log_path,
                    "finished_at": j.finished_at,
                })
        return templates.TemplateResponse(
            request, "system_logs.html", _ctx("system/logs", tab="jobs", jobs=rows),
        )
    return templates.TemplateResponse(
        request, "system_logs.html", _ctx("system/logs", tab="app", lines=_app_log_lines()),
    )


def _app_log_lines(limit: int = 500) -> list[str]:
    from ..main import LOG_RING
    # `LOG_RING` is a deque — slice the last `limit` entries.
    return list(LOG_RING)[-limit:]


@router.get("/system/logs/app/fragment", response_class=HTMLResponse)
async def system_logs_app_fragment(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_app_log_tail.html", {"lines": _app_log_lines()},
    )


@router.get("/system/logs/app/download")
async def system_logs_app_download() -> FileResponse:
    """Stream the rotating file. The in-memory ring is for live tailing — the
    file is the persistent record across restarts."""
    log_path = settings.absolute_data_dir / "logs" / "convertarr.log"
    if not log_path.exists():
        raise HTTPException(404, "no log file yet")
    return FileResponse(
        log_path, media_type="text/plain",
        filename=f"convertarr-{datetime.now(timezone.utc):%Y%m%d}.log",
    )


@router.get("/system/logs/job/{job_id}", response_class=Response)
async def system_logs_job(job_id: int) -> Response:
    """Return the raw ffmpeg stderr log for a specific job. Used by the Jobs
    sub-tab's modal (loaded via fetch())."""
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or not j.log_path:
            raise HTTPException(404, "job has no log")
        log_path = Path(j.log_path)
    if not log_path.exists():
        raise HTTPException(404, "log file gone")
    # Tail the last 200 KB so we don't ship a 5 MB ffmpeg dump for nothing.
    size = log_path.stat().st_size
    with log_path.open("rb") as fh:
        if size > 200_000:
            fh.seek(size - 200_000)
        body = fh.read()
    return Response(content=body, media_type="text/plain")


# ---------- Backup ----------

@router.get("/system/backup", response_class=HTMLResponse)
async def system_backup(request: Request, status: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "system_backup.html", _ctx("system/backup", status=status),
    )


def _config_payload() -> dict:
    """Snapshot of every user-editable thing in the DB. Excludes media_file,
    job, image_cache (all derivable / regenerable)."""
    with session_scope() as s:
        instances = []
        for i in s.scalars(select(ArrInstance).order_by(ArrInstance.id)).all():
            instances.append({
                "kind": i.kind.value,
                "name": i.name,
                "address": i.address,
                "port": i.port,
                "base_path": i.base_path,
                "use_ssl": bool(i.use_ssl),
                "http_timeout": i.http_timeout,
                "api_key": i.api_key,
                "enabled": i.enabled,
                "mappings": [
                    {"remote": m.remote_path, "local": m.local_path}
                    for m in i.path_mappings
                ],
            })
        saved_filters = [
            {"scope": f.scope, "name": f.name, "clauses": f.clauses or []}
            for f in s.scalars(select(SavedFilter).order_by(SavedFilter.id)).all()
        ]
        workflows = [
            {
                "name": w.name, "enabled": bool(w.enabled), "priority": w.priority,
                "conditions": list(w.conditions or []),
                "target_video_codec": w.target_video_codec,
                "target_audio_codec": w.target_audio_codec,
            }
            for w in s.scalars(select(Workflow).order_by(Workflow.priority, Workflow.id)).all()
        ]
    from .. import __version__ as _convertarr_version
    return {
        "convertarr_version": _convertarr_version,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "instances": instances,
        "saved_filters": saved_filters,
        "workflows": workflows,
        "settings": rs.all(),
    }


@router.get("/system/backup/export")
async def system_backup_export() -> Response:
    payload = _config_payload()
    body = json.dumps(payload, indent=2).encode("utf-8")
    return Response(
        content=body,
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="convertarr-config-{datetime.now(timezone.utc):%Y%m%d}.json"',
        },
    )


@router.post("/system/backup/import")
async def system_backup_import(file: UploadFile = File(...)) -> RedirectResponse:
    raw = await file.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return RedirectResponse("/system/backup?status=invalid", status_code=303)
    if not isinstance(payload, dict) or "instances" not in payload:
        return RedirectResponse("/system/backup?status=invalid", status_code=303)

    # Replace instances + mappings + saved filters + workflows + settings;
    # leave job/media_file rows untouched so history isn't wiped.
    with session_scope() as s:
        for old in s.scalars(select(PathMapping)).all():
            s.delete(old)
        for old in s.scalars(select(ArrInstance)).all():
            s.delete(old)
        for old in s.scalars(select(SavedFilter)).all():
            s.delete(old)
        for old in s.scalars(select(Workflow)).all():
            s.delete(old)
        s.flush()

        for inst in payload.get("instances", []):
            kind = inst.get("kind")
            if kind not in ("sonarr", "radarr"):
                continue
            row = ArrInstance(
                kind=ArrKind(kind),
                name=inst.get("name", "imported"),
                address=inst.get("address", ""),
                port=int(inst.get("port", 0)),
                base_path=inst.get("base_path", "/"),
                use_ssl=bool(inst.get("use_ssl", False)),
                http_timeout=int(inst.get("http_timeout", 60)),
                api_key=inst.get("api_key", ""),
                enabled=bool(inst.get("enabled", True)),
            )
            s.add(row)
            s.flush()
            for m in inst.get("mappings", []):
                s.add(PathMapping(
                    arr_instance_id=row.id,
                    remote_path=m.get("remote", ""),
                    local_path=m.get("local", ""),
                ))

        for f in payload.get("saved_filters", []):
            s.add(SavedFilter(
                scope=f.get("scope", "series"),
                name=f.get("name", "imported"),
                clauses=f.get("clauses", []),
            ))

        for w in payload.get("workflows", []):
            s.add(Workflow(
                name=w.get("name", "imported"),
                enabled=bool(w.get("enabled", True)),
                priority=int(w.get("priority", 100)),
                conditions=w.get("conditions", []),
                target_video_codec=w.get("target_video_codec", "hevc"),
                target_audio_codec=w.get("target_audio_codec", "aac"),
            ))

    # Settings: restore the user-editable ones, but never overwrite the api_key
    # or password hash from a file (would be a footgun on misuse).
    safe_settings = (payload.get("settings") or {})
    for k, v in safe_settings.items():
        if k in ("api_key", "auth_password_hash"):
            continue
        rs.set(k, v)

    return RedirectResponse("/system/backup?status=imported", status_code=303)


# ---------- Statistics ----------

# Codec display helpers (canonical_codec) live in probe/codec_labels so both
# the statistics charts here and the dashboard chips can share them.


def _resolution_bucket(height: int | None) -> str:
    """Sonarr-style resolution buckets keyed on the video stream's height."""
    if not height:
        return "Unknown"
    if height >= 2000:
        return "4K (2160p)"
    if height >= 1400:
        return "1440p"
    if height >= 1000:
        return "1080p"
    if height >= 700:
        return "720p"
    if height >= 540:
        return "576p"
    if height >= 350:
        return "480p"
    return "SD"


_RESOLUTION_ORDER = [
    "4K (2160p)", "1440p", "1080p", "720p", "576p", "480p", "SD", "Unknown",
]


def _format_duration(seconds: float | None) -> str:
    """Human-friendly: '45s', '2m 30s', '1h 12m'."""
    if seconds is None or seconds < 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def _job_durations_by_kind() -> dict[str, list[float]]:
    """Walk every successful job, joining MediaFile to discover whether the
    job was a Sonarr episode or a Radarr movie. Returns the per-kind list of
    encode wall-clock durations in seconds.

    Outer-joined on MediaFile so worker-side mirror rows (which have no
    local MediaFile) still count toward `total_runtime` — they bucket as
    `unknown` since we don't ship arr_kind across the dispatch.
    """
    out: dict[str, list[float]] = {"sonarr": [], "radarr": [], "unknown": []}
    with session_scope() as s:
        rows = s.execute(
            select(Job.started_at, Job.finished_at, MediaFile.arr_kind)
            .outerjoin(MediaFile, MediaFile.id == Job.media_file_id)
            .where(
                Job.state == JobState.done,
                Job.started_at.is_not(None),
                Job.finished_at.is_not(None),
            )
        ).all()
    for started_at, finished_at, kind in rows:
        if started_at is None or finished_at is None:
            continue
        seconds = (finished_at - started_at).total_seconds()
        if seconds <= 0:
            continue
        bucket = "sonarr" if kind == ArrKind.sonarr else "radarr" if kind == ArrKind.radarr else "unknown"
        out[bucket].append(seconds)
    return out


def _to_chart_rows(counter: dict[str, int]) -> list[dict]:
    """Counter → list of {label, count, pct} sorted by count desc. Pct is
    relative to the largest bucket so the longest bar is always 100%."""
    if not counter:
        return []
    top = max(counter.values())
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"label": k, "count": v, "pct": round(100 * v / top) if top else 0}
        for k, v in items
    ]


def _resolution_chart_rows(counter: dict[str, int]) -> list[dict]:
    """Like _to_chart_rows but preserves the natural 4K → SD ordering instead
    of sorting by count, so the chart reads top-down by quality."""
    if not counter:
        return []
    top = max(counter.values())
    rows: list[dict] = []
    for label in _RESOLUTION_ORDER:
        if label not in counter:
            continue
        v = counter[label]
        rows.append({"label": label, "count": v, "pct": round(100 * v / top) if top else 0})
    return rows


def _statistics_payload() -> dict:
    """Compute everything the /system/statistics page renders. Pure DB
    work — runs on every request, but the totals are small (one query per
    section, no per-row N+1)."""

    # ---- Conversion stats ----
    durations = _job_durations_by_kind()
    sonarr_secs = durations["sonarr"]
    radarr_secs = durations["radarr"]
    avg_sonarr = sum(sonarr_secs) / len(sonarr_secs) if sonarr_secs else None
    avg_radarr = sum(radarr_secs) / len(radarr_secs) if radarr_secs else None
    total_runtime = sum(sonarr_secs) + sum(radarr_secs) + sum(durations["unknown"])

    with session_scope() as s:
        state_counts = dict(
            s.execute(
                select(Job.state, func.count(Job.id)).group_by(Job.state)
            ).all()
        )
        encoder_counts = dict(
            s.execute(
                select(Job.encoder, func.count(Job.id))
                .where(Job.state == JobState.done, Job.encoder.is_not(None))
                .group_by(Job.encoder)
            ).all()
        )

    done = state_counts.get(JobState.done, 0)
    failed = state_counts.get(JobState.failed, 0)
    skipped = state_counts.get(JobState.skipped, 0)
    queued = state_counts.get(JobState.queued, 0)
    running = state_counts.get(JobState.running, 0)

    # ---- Library: codec + container distribution from EntityIndex ----
    codec_counts: dict[str, int] = {}
    format_counts: dict[str, int] = {}
    sonarr_entities = 0
    radarr_entities = 0

    with session_scope() as s:
        ei_rows = s.execute(
            select(EntityIndex.arr_kind, EntityIndex.formats, EntityIndex.video_codecs)
        ).all()
    for kind, formats, codecs in ei_rows:
        if kind == ArrKind.sonarr:
            sonarr_entities += 1
        elif kind == ArrKind.radarr:
            radarr_entities += 1
        # Dedupe canonical codec names within an entity so a single h264
        # file doesn't get counted three times for ['avc', 'h264', 'x264'].
        seen_codec: set[str] = set()
        for c in codecs or []:
            canonical = _canonical_codec(c)
            if canonical in seen_codec:
                continue
            seen_codec.add(canonical)
            codec_counts[canonical] = codec_counts.get(canonical, 0) + 1
        for f in formats or []:
            label = (f or "").lower()
            if not label:
                continue
            format_counts[label] = format_counts.get(label, 0) + 1

    # ---- Library: resolution from MediaFile probes ----
    res_counts: dict[str, int] = {}
    probed_files = 0
    with session_scope() as s:
        probes = s.execute(
            select(MediaFile.probe_json).where(MediaFile.probe_json.is_not(None))
        ).all()
    for (probe,) in probes:
        height = None
        for st in (probe or {}).get("streams") or []:
            if st.get("codec_type") != "video":
                continue
            if (st.get("disposition") or {}).get("attached_pic"):
                continue
            height = st.get("height") or st.get("coded_height")
            if height:
                break
        bucket = _resolution_bucket(int(height) if height else None)
        res_counts[bucket] = res_counts.get(bucket, 0) + 1
        probed_files += 1

    return {
        "conversion": {
            "done": done,
            "failed": failed,
            "skipped": skipped,
            "queued": queued,
            "running": running,
            "avg_sonarr_str": _format_duration(avg_sonarr),
            "avg_radarr_str": _format_duration(avg_radarr),
            "sonarr_sample": len(sonarr_secs),
            "radarr_sample": len(radarr_secs),
            "total_runtime_str": _format_duration(total_runtime if total_runtime else None),
            "encoders": _to_chart_rows({k: v for k, v in encoder_counts.items() if k}),
        },
        "library": {
            "series_count": sonarr_entities,
            "movie_count": radarr_entities,
            "probed_files": probed_files,
            "codecs": _to_chart_rows(codec_counts),
            "containers": _to_chart_rows(format_counts),
            "resolutions": _resolution_chart_rows(res_counts),
        },
    }


@router.get("/system/statistics", response_class=HTMLResponse)
async def system_statistics(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "system_statistics.html",
        _ctx("system/statistics", **_statistics_payload()),
    )


# ---------- About ----------

def _format_uptime(seconds: float) -> str:
    """`HH:MM:SS` for short uptimes; switches to `Nd HH:MM:SS` after a day."""
    s = int(max(0, seconds))
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{s:02d}"
    return f"{hours:02d}:{minutes:02d}:{s:02d}"


def _about_payload() -> dict:
    """Static-ish runtime info for the About page. Sonarr-style: version,
    runtime, database, paths, uptime."""
    import sqlite3
    import sys

    from .. import __version__ as _convertarr_version
    from ..main import STARTED_AT

    started = STARTED_AT
    now = datetime.now(timezone.utc)
    uptime_seconds = (now - started).total_seconds()

    # Resolve the SQLite file from the configured db_url. We support the
    # default sqlite:/// scheme; anything else falls back to showing the URL
    # as-is so the page never breaks on exotic configurations.
    db_url = settings.db_url
    if db_url.startswith("sqlite:///"):
        rel = db_url[len("sqlite:///"):]
        db_file = (settings.project_root / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    else:
        db_file = Path(db_url)

    py_version = ".".join(str(v) for v in sys.version_info[:3])

    return {
        "convertarr_version": _convertarr_version,
        "python_version": py_version,
        "sqlite_version": sqlite3.sqlite_version,
        "appdata_dir": str(settings.absolute_data_dir),
        "database_file": str(db_file),
        "startup_dir": str(settings.project_root),
        "mode": "Console",
        "started_at": started,
        "uptime_str": _format_uptime(uptime_seconds),
    }


@router.get("/system/about", response_class=HTMLResponse)
async def system_about(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "system_about.html",
        _ctx("system/about", **_about_payload()),
    )
