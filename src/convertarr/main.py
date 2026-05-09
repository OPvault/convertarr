from __future__ import annotations

import asyncio
import logging
import sys
from collections import deque
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .web.auth import AuthRedirect
from .web.auth_routes import router as auth_router
from .web.node_routes import router as node_router
from .web.pairing_routes import router as pairing_router
from .web.routes import router
from .web.system_routes import router as system_router
from .workers.heartbeat import watchdog_loop
from .workers.local_node import ensure_local_node
from .workers.supervisor import supervisor_loop


# In-memory ring buffer of recent log lines, surfaced by the System → Logs page.
LOG_RING: deque[str] = deque(maxlen=2000)

# Process start time, used by /system/about for the Uptime field.
import datetime as _dt
STARTED_AT: _dt.datetime = _dt.datetime.now(_dt.timezone.utc)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_RING.append(self.format(record))
        except Exception:
            pass


def _configure_logging() -> None:
    """Send Python logging to stderr + a rotating file + an in-memory ring buffer.

    The ring buffer powers the in-app log viewer; the file is downloadable from
    the same page; stderr keeps things visible during direct uvicorn runs.
    """
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_path = settings.absolute_data_dir / "logs" / "convertarr.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    ring = _RingHandler()
    ring.setFormatter(fmt)
    root.addHandler(ring)


_configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Clean up any `.CONVERTARR.` output files left behind by crashed/killed
    # conversions before the worker starts picking up new jobs.
    from .workers.cleanup import cleanup_orphaned_outputs
    removed = cleanup_orphaned_outputs()
    if removed:
        logging.getLogger(__name__).warning(
            "startup cleanup: removed %d orphaned converter output(s)", removed,
        )
    # Upsert the local Node row before the worker loops start. Existing rows
    # have their encoder fields refreshed; the row is the source of truth for
    # the host's per-node max_concurrent_jobs from this point on.
    ensure_local_node()

    from .workers.indexer import indexer_loop
    stop = asyncio.Event()
    # The supervisor decides whether to run host-mode (local worker loop) or
    # worker-mode (remote loop connected to a paired host). It also reacts
    # to live pairing/unpairing without a process restart.
    task = asyncio.create_task(supervisor_loop(stop), name="convertarr-supervisor")
    indexer_task = asyncio.create_task(indexer_loop(stop), name="convertarr-indexer")
    watchdog_task = asyncio.create_task(watchdog_loop(stop), name="convertarr-watchdog")
    app.state.worker_stop = stop
    app.state.worker_task = task
    app.state.indexer_task = indexer_task
    app.state.watchdog_task = watchdog_task
    try:
        yield
    finally:
        stop.set()
        for t in (task, indexer_task, watchdog_task):
            try:
                await asyncio.wait_for(t, timeout=10)
            except asyncio.TimeoutError:
                t.cancel()


app = FastAPI(title="Convertarr", lifespan=lifespan)


@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(_request: Request, exc: AuthRedirect) -> RedirectResponse:
    return RedirectResponse(exc.location, status_code=303)


app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")
# Public routes (login, setup, logout) — no auth dependency.
app.include_router(auth_router)
# Protected routes — require_auth gate is on the router itself.
app.include_router(router)
app.include_router(system_router)
# Worker-node API (consumed by paired worker instances); same X-Api-Key auth.
app.include_router(node_router)
# Pairing API — invoked by another Convertarr (the host) to enslave this
# instance as a worker. Auth is the LOCAL api_key (the one the operator
# typed into the host's pair form), enforced via the same require_auth.
app.include_router(pairing_router)


HOST = "0.0.0.0"
PORT = 6565


def cli() -> None:
    """Console-script entrypoint. Always binds 0.0.0.0:6565 — Convertarr is
    LAN-reachable by default, Sonarr/Radarr-style.

    Pass `--reload` for dev mode (auto-restart on code changes)."""
    init_db()
    reload = "--reload" in sys.argv

    import uvicorn
    kwargs: dict = {"host": HOST, "port": PORT, "reload": reload, "log_config": None}
    if reload:
        # Only watch source. Without this, every DB write (SQLite WAL) and every
        # log line (data/logs/convertarr.log) trips the reloader → reload loop.
        src_dir = Path(__file__).resolve().parent.parent
        kwargs["reload_dirs"] = [str(src_dir)]
    uvicorn.run("convertarr.main:app", **kwargs)


def run() -> None:
    """Backwards-compatible alias for cli()."""
    cli()


if __name__ == "__main__":
    cli()
