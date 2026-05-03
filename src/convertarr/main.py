from __future__ import annotations

import asyncio
import logging
import os
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
from .web.routes import router
from .web.system_routes import router as system_router
from .workers.queue import worker_loop


# In-memory ring buffer of recent log lines, surfaced by the System → Logs page.
LOG_RING: deque[str] = deque(maxlen=2000)


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
    # Reset any handlers from a prior init (matters for the os.execv re-run).
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
    stop = asyncio.Event()
    task = asyncio.create_task(worker_loop(stop), name="convertarr-worker")
    app.state.worker_stop = stop
    app.state.worker_task = task
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()


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


# Set by cli() so schedule_restart() can tell the difference between a
# "convertarr"-script launch (re-exec works) and a bare-uvicorn dev launch
# (re-exec either crashes on a stale socket or no-ops because cli() is bypassed
# and the new bind_address is never read).
_LAUNCHED_VIA_CLI = False


def schedule_restart(delay: float = 0.5) -> bool:
    """Re-exec this process so DB-backed config (bind address, etc.) re-reads.

    Returns True if a restart was scheduled, False if we declined because we're
    not running under the `convertarr` cli (e.g. `uvicorn --reload` dev mode).
    Only the `convertarr` entrypoint reads `bind_address` from the DB, so
    re-execing anything else would either crash on a stale listening socket or
    silently ignore the new value."""
    log = logging.getLogger(__name__)

    if not _LAUNCHED_VIA_CLI:
        log.warning(
            "bind_address change saved but not applied: not launched via the "
            "`convertarr` script. Stop the current process and run `convertarr` "
            "for the new address to take effect."
        )
        return False

    async def _later() -> None:
        await asyncio.sleep(delay)
        log.warning("re-execing for config reload: %s %s", sys.argv[0], sys.argv[1:])
        os.execv(sys.argv[0], sys.argv)

    asyncio.create_task(_later())
    return True


def cli() -> None:
    """Console-script entrypoint. Reads bind from the settings table so the UI
    can change it (and `schedule_restart()` can pick up the new value).

    Pass `--reload` for dev mode (auto-restart on code changes). Note: in
    `--reload` mode, schedule_restart's re-exec is unreliable because uvicorn's
    reload-worker child holds the listening socket; the bind_address change is
    still saved but you'll get a manual-restart prompt in the UI."""
    global _LAUNCHED_VIA_CLI
    _LAUNCHED_VIA_CLI = True
    init_db()
    from .web import runtime_settings as rs

    bind = rs.get("bind_address", "0.0.0.0:8000")
    host, _, port_s = bind.partition(":")
    port = int(port_s or "8000")
    reload = "--reload" in sys.argv

    if reload:
        # Reload mode forks workers; our re-exec restart trick fights the
        # watcher and ends up with "Address already in use". Disable it.
        _LAUNCHED_VIA_CLI = False

    import uvicorn
    kwargs: dict = {"host": host, "port": port, "reload": reload, "log_config": None}
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
