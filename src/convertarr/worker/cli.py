"""`convertarr-worker` console-script entrypoint.

Usage:

    convertarr-worker --host http://hostname:6565 --api-key XXX [options]

Or env-var driven:

    CONVERTARR_HOST=http://hostname:6565
    CONVERTARR_API_KEY=XXX
    CONVERTARR_NODE_NAME=desktop
    CONVERTARR_MAX_JOBS=4
    convertarr-worker

Auto-detects the local encoder (NVENC / VAAPI / QSV / AMF / CPU) and
advertises it on register. Polls the host every 15s; claims new work as
slots free up.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .client import WorkerClient
from .config import default_config_dir, load_or_create_node_id
from .loop import WorkerLoop

log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="convertarr-worker")
    p.add_argument(
        "--host",
        default=os.environ.get("CONVERTARR_HOST"),
        help="Host URL, e.g. http://convertarr.lan:6565 (env: CONVERTARR_HOST)",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("CONVERTARR_API_KEY"),
        help="API key for X-Api-Key auth (env: CONVERTARR_API_KEY). "
             "Find this in Settings → General on the host.",
    )
    p.add_argument(
        "--name",
        default=os.environ.get("CONVERTARR_NODE_NAME"),
        help="Display name for this worker in the host UI "
             "(env: CONVERTARR_NODE_NAME, default: hostname)",
    )
    p.add_argument(
        "--max-jobs",
        type=int,
        default=int(os.environ.get("CONVERTARR_MAX_JOBS", "1")),
        help="Concurrent encode slots (env: CONVERTARR_MAX_JOBS, default 1). "
             "Can also be changed live in the host's Nodes UI.",
    )
    p.add_argument(
        "--config-dir",
        default=os.environ.get("CONVERTARR_WORKER_CONFIG_DIR"),
        help="Where to store the persistent node-id "
             "(env: CONVERTARR_WORKER_CONFIG_DIR, default: ~/.config/convertarr-worker)",
    )
    return p


def _configure_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)


def worker_cli() -> None:
    _configure_logging()
    args = _build_parser().parse_args()

    if not args.host:
        sys.stderr.write("error: --host is required (or set CONVERTARR_HOST)\n")
        sys.exit(2)
    if not args.api_key:
        sys.stderr.write("error: --api-key is required (or set CONVERTARR_API_KEY)\n")
        sys.exit(2)

    config_dir = Path(args.config_dir) if args.config_dir else default_config_dir()
    node_id = load_or_create_node_id(config_dir)
    name = args.name or os.uname().nodename or "worker"

    log.info(
        "starting convertarr-worker name=%s id=%s host=%s max_jobs=%d",
        name, node_id, args.host, args.max_jobs,
    )

    client = WorkerClient(args.host, args.api_key)
    loop = WorkerLoop(
        client=client,
        node_id=node_id,
        node_name=name,
        max_jobs_fallback=args.max_jobs,
    )

    stop = asyncio.Event()

    def _handle_signal(_signum, _frame):
        log.info("received shutdown signal")
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        asyncio.run(loop.run_forever(stop))
    except KeyboardInterrupt:
        pass
