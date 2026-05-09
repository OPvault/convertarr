"""Persistent worker config — just the stable node_id, kept across restarts.

Default location: `$XDG_CONFIG_HOME/convertarr-worker/node-id` (or
`~/.config/convertarr-worker/node-id`). Override with `--config-dir` on the
CLI or `CONVERTARR_WORKER_CONFIG_DIR` in the env.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def default_config_dir() -> Path:
    env = os.environ.get("CONVERTARR_WORKER_CONFIG_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "convertarr-worker"
    return Path.home() / ".config" / "convertarr-worker"


def load_or_create_node_id(config_dir: Path | None = None) -> str:
    """Read the node-id file, generating one on first run. Idempotent."""
    cfg = config_dir or default_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    node_id_file = cfg / "node-id"
    if node_id_file.exists():
        existing = node_id_file.read_text().strip()
        if existing:
            return existing
    new_id = str(uuid.uuid4())
    node_id_file.write_text(new_id + "\n")
    return new_id
