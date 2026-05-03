from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

log = logging.getLogger(__name__)


def _apply_sqlite_pragmas(dbapi_connection, _record) -> None:
    """Enable WAL mode + relaxed sync so the worker's per-progress writes don't
    block the polling reader. Without WAL, SQLite serializes readers behind the
    writer's exclusive lock — which makes live progress polling stutter. With
    WAL the reader sees a consistent snapshot while the writer keeps appending.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _resolve_db_url() -> str:
    url = settings.db_url
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        rel = url[len("sqlite:///") :]
        abs_path = settings.project_root / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{abs_path}"
    return url


engine = create_engine(_resolve_db_url(), echo=False, future=True, connect_args={"check_same_thread": False})
event.listen(engine, "connect", _apply_sqlite_pragmas)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _migrate_arr_instance(engine: Engine) -> None:
    """Add Bazarr-style fields to arr_instance, backfill from legacy base_url,
    and drop the legacy column once data is migrated.

    SQLAlchemy's create_all does not ALTER existing tables — this is a tiny
    one-shot migration so existing rows (the user's connected Sonarr instance
    + path mappings) survive the schema change.
    """
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(arr_instance)").fetchall()}
        if not cols:
            return  # table doesn't exist yet — create_all will make it fresh

        new_cols_missing = "address" not in cols
        legacy_has_base_url = "base_url" in cols

        if new_cols_missing:
            log.info("migrating arr_instance: adding address/port/base_path/use_ssl/http_timeout")
            for col, ddl in [
                ("address", "TEXT NOT NULL DEFAULT ''"),
                ("port", "INTEGER NOT NULL DEFAULT 0"),
                ("base_path", "TEXT NOT NULL DEFAULT '/'"),
                ("use_ssl", "BOOLEAN NOT NULL DEFAULT 0"),
                ("http_timeout", "INTEGER NOT NULL DEFAULT 60"),
            ]:
                conn.exec_driver_sql(f"ALTER TABLE arr_instance ADD COLUMN {col} {ddl}")

        if legacy_has_base_url:
            # Backfill from legacy base_url for any row that hasn't been migrated yet.
            # Safe to run repeatedly: only updates rows where address is empty.
            for row_id, base_url, address in conn.exec_driver_sql(
                "SELECT id, base_url, address FROM arr_instance"
            ).fetchall():
                if address:  # already populated
                    continue
                if not base_url:
                    continue
                u = urlparse(base_url)
                host = u.hostname or ""
                port = u.port or (443 if u.scheme == "https" else 80)
                path = u.path or "/"
                use_ssl = 1 if u.scheme == "https" else 0
                conn.exec_driver_sql(
                    "UPDATE arr_instance SET address=?, port=?, base_path=?, use_ssl=? WHERE id=?",
                    (host, port, path, use_ssl, row_id),
                )
                log.info("migrated instance %d: %s -> %s:%d (ssl=%d, base=%s)",
                         row_id, base_url, host, port, use_ssl, path)

            # Drop the legacy column so future INSERTs don't have to populate it.
            # Requires SQLite >= 3.35; we ship with much newer.
            log.info("dropping legacy arr_instance.base_url column")
            conn.exec_driver_sql("ALTER TABLE arr_instance DROP COLUMN base_url")


def init_db() -> None:
    _migrate_arr_instance(engine)
    Base.metadata.create_all(engine)
    # Seed runtime settings (api_key, bind_address, auth defaults, delete_originals).
    # Imported lazily because runtime_settings imports from .db.
    from .web.runtime_settings import seed_defaults_if_missing
    seed_defaults_if_missing()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
