"""Typed access to the `setting` table — values are JSON-encoded so we can
round-trip booleans, ints, and strings without per-key parsing.

Read-heavy paths (auth checks, finalize-swap) hit this on every request, so
keep it cheap: each call opens a short SQLAlchemy session, no caching layer.
SQLite + WAL handles the concurrent reads fine.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from ..db import session_scope
from ..models import Setting

log = logging.getLogger(__name__)


# Sentinel separate from None so callers can store None as a real value.
_MISSING = object()


def get(key: str, default: Any = _MISSING) -> Any:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None:
            if default is _MISSING:
                raise KeyError(key)
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            # Tolerate raw strings stored before JSON-encoding was a thing.
            return row.value


def set(key: str, value: Any) -> None:
    encoded = json.dumps(value)
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=encoded, updated_at=datetime.now(timezone.utc)))
        else:
            row.value = encoded
            row.updated_at = datetime.now(timezone.utc)


def all() -> dict[str, Any]:
    """Returns every setting as a plain dict. Used by the export-config endpoint."""
    out: dict[str, Any] = {}
    with session_scope() as s:
        for row in s.scalars(select(Setting)).all():
            try:
                out[row.key] = json.loads(row.value)
            except (json.JSONDecodeError, TypeError):
                out[row.key] = row.value
    return out


# ---- Default-seeding ----

DEFAULT_KEYS: dict[str, Any] = {
    "api_key": None,            # populated to a random hex on first seed
    "auth_method": "none",      # "none" | "basic" | "forms" — Sonarr-style default
    "auth_username": "admin",
    "auth_password_hash": "",   # empty triggers /setup once auth_method != "none"
    # Default ON: the worker only invokes the swap-and-delete path after a
    # successful encode (`elif success:` in queue._run_one_job), so failed
    # or cancelled jobs always leave the original untouched. Cancelled jobs
    # additionally clean up their partial output before exiting.
    "delete_originals": True,
}


def seed_defaults_if_missing() -> None:
    """Populate the `setting` table with defaults for any key that's not already
    present. Never overwrites an existing user-configured value. Generates a
    fresh `api_key` on first seed."""
    with session_scope() as s:
        existing = {row.key for row in s.scalars(select(Setting)).all()}
        for key, default in DEFAULT_KEYS.items():
            if key in existing:
                continue
            value = secrets.token_hex(32) if key == "api_key" else default
            s.add(Setting(
                key=key,
                value=json.dumps(value),
                updated_at=datetime.now(timezone.utc),
            ))
            log.info("seeded default setting: %s", key)
