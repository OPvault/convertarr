"""System pages — Logs (app + per-job) and Backup (config export/import)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..config import settings
from ..db import session_scope
from ..models import ArrInstance, ArrKind, Job, MediaFile, PathMapping, SavedFilter
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
                mf = s.get(MediaFile, j.media_file_id)
                rows.append({
                    "id": j.id,
                    "state": j.state.value,
                    "title": (mf.arr_entity_title if mf else "") or "",
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
    return {
        "convertarr_version": "0.1.0",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "instances": instances,
        "saved_filters": saved_filters,
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

    # Replace instances + mappings + saved filters + settings; leave job/media_file
    # rows untouched so history isn't wiped.
    with session_scope() as s:
        for old in s.scalars(select(PathMapping)).all():
            s.delete(old)
        for old in s.scalars(select(ArrInstance)).all():
            s.delete(old)
        for old in s.scalars(select(SavedFilter)).all():
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

    # Settings: restore the user-editable ones, but never overwrite the api_key
    # or password hash from a file (would be a footgun on misuse).
    safe_settings = (payload.get("settings") or {})
    for k, v in safe_settings.items():
        if k in ("api_key", "auth_password_hash"):
            continue
        rs.set(k, v)

    return RedirectResponse("/system/backup?status=imported", status_code=303)
