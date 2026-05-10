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


def _migrate_job_node_id(engine: Engine) -> None:
    """Add `job.node_id` to existing installs. Multi-node feature attaches
    every Job to a Node; pre-existing rows have NULL until they're (re)claimed.
    Same pattern as `_migrate_arr_instance` — SQLAlchemy's create_all doesn't
    ALTER existing tables.
    """
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job)").fetchall()}
        if not cols:
            return  # table doesn't exist yet — create_all will make it fresh
        if "node_id" not in cols:
            log.info("migrating job: adding node_id column")
            conn.exec_driver_sql("ALTER TABLE job ADD COLUMN node_id VARCHAR(64)")
            conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_job_node_id ON job(node_id)")


def _migrate_node_pair_columns(engine: Engine) -> None:
    """Add pair_url + pair_api_key to the node table for installs that
    already have a `node` table from the previous multi-node iteration."""
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(node)").fetchall()}
        if not cols:
            return  # table doesn't exist yet
        if "pair_url" not in cols:
            log.info("migrating node: adding pair_url + pair_api_key columns")
            conn.exec_driver_sql("ALTER TABLE node ADD COLUMN pair_url VARCHAR(255)")
            conn.exec_driver_sql("ALTER TABLE node ADD COLUMN pair_api_key VARCHAR(200)")


def _migrate_job_mirror_columns(engine: Engine) -> None:
    """Add the worker-side mirror columns to `job` and relax the NOT NULL on
    `media_file_id` so worker mirror rows (which don't reference a local
    MediaFile) can be inserted. Idempotent — checks the table_info before
    each step.
    """
    with engine.begin() as conn:
        info = conn.exec_driver_sql("PRAGMA table_info(job)").fetchall()
        if not info:
            return  # table doesn't exist yet — create_all will make it fresh

        cols = {row[1] for row in info}
        for col, ddl in [
            ("host_job_id", "INTEGER"),
            ("display_title", "VARCHAR(500)"),
            ("source_path", "VARCHAR(2000)"),
            ("node_name", "VARCHAR(120)"),
        ]:
            if col not in cols:
                log.info("migrating job: adding %s column", col)
                conn.exec_driver_sql(f"ALTER TABLE job ADD COLUMN {col} {ddl}")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_job_host_job_id ON job(host_job_id)"
        )

        # Relax NOT NULL on media_file_id if needed. SQLite can't ALTER a
        # column's nullability — we do the standard rename/recreate dance.
        # `notnull` is column 3 in PRAGMA table_info output.
        info = conn.exec_driver_sql("PRAGMA table_info(job)").fetchall()
        mf_col = next((r for r in info if r[1] == "media_file_id"), None)
        if mf_col is not None and mf_col[3] == 1:
            log.info("migrating job: relaxing media_file_id NOT NULL constraint")
            # Use the metadata's CREATE TABLE statement on a temp name, copy,
            # swap. Hand-rolled DDL to mirror the current Job shape.
            conn.exec_driver_sql("ALTER TABLE job RENAME TO _job_old")
            conn.exec_driver_sql(
                """
                CREATE TABLE job (
                    id INTEGER PRIMARY KEY,
                    media_file_id INTEGER REFERENCES media_file(id),
                    state VARCHAR(20) NOT NULL,
                    node_id VARCHAR(64) REFERENCES node(id),
                    host_job_id INTEGER,
                    display_title VARCHAR(500),
                    source_path VARCHAR(2000),
                    node_name VARCHAR(120),
                    output_path VARCHAR(2000),
                    encoder VARCHAR(60),
                    ffmpeg_args JSON,
                    progress_pct FLOAT NOT NULL DEFAULT 0.0,
                    progress_speed FLOAT,
                    progress_fps FLOAT,
                    log_path VARCHAR(2000),
                    error_tail TEXT,
                    created_at DATETIME NOT NULL,
                    started_at DATETIME,
                    finished_at DATETIME
                )
                """
            )
            conn.exec_driver_sql(
                """
                INSERT INTO job (
                    id, media_file_id, state, node_id, host_job_id,
                    display_title, source_path, node_name,
                    output_path, encoder, ffmpeg_args,
                    progress_pct, progress_speed, progress_fps,
                    log_path, error_tail, created_at, started_at, finished_at
                )
                SELECT
                    id, media_file_id, state, node_id, host_job_id,
                    display_title, source_path, node_name,
                    output_path, encoder, ffmpeg_args,
                    progress_pct, progress_speed, progress_fps,
                    log_path, error_tail, created_at, started_at, finished_at
                FROM _job_old
                """
            )
            conn.exec_driver_sql("DROP TABLE _job_old")
            conn.exec_driver_sql("CREATE INDEX ix_job_state ON job(state)")
            conn.exec_driver_sql("CREATE INDEX ix_job_node_id ON job(node_id)")
            conn.exec_driver_sql(
                "CREATE INDEX ix_job_host_job_id ON job(host_job_id)"
            )


def _migrate_media_file_original_path(engine: Engine) -> None:
    """Add `media_file.arr_original_path` so remote workers can apply their
    own arr PathMapping rows to translate paths. Existing rows have NULL
    (unknown — only ingest captures the original); callers fall back to
    `path` when this is empty.
    """
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_file)").fetchall()}
        if not cols:
            return
        if "arr_original_path" not in cols:
            log.info("migrating media_file: adding arr_original_path column")
            conn.exec_driver_sql("ALTER TABLE media_file ADD COLUMN arr_original_path VARCHAR(2000)")


def _migrate_media_file_episode_columns(engine: Engine) -> None:
    """Add `media_file.season_number` + `episode_number` so Sonarr ingests can
    label episodes individually on the dashboard. Existing rows have NULL —
    the dashboard falls back to series-name-only until the next ingest pass
    populates them.
    """
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(media_file)").fetchall()}
        if not cols:
            return
        for col in ("season_number", "episode_number"):
            if col not in cols:
                log.info("migrating media_file: adding %s column", col)
                conn.exec_driver_sql(f"ALTER TABLE media_file ADD COLUMN {col} INTEGER")


def _migrate_job_codec_columns(engine: Engine) -> None:
    """Add the source_*_codec / target_*_codec snapshot columns to `job`.
    Stamped at claim time so the dashboard can render a 'AV1 → HEVC'
    chip without re-evaluating the workflow on every poll. Idempotent —
    same pattern as `_migrate_job_mirror_columns`.
    """
    with engine.begin() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(job)").fetchall()}
        if not cols:
            return  # table doesn't exist yet — create_all will make it fresh
        for col in (
            "source_video_codec",
            "source_audio_codec",
            "target_video_codec",
            "target_audio_codec",
        ):
            if col not in cols:
                log.info("migrating job: adding %s column", col)
                conn.exec_driver_sql(f"ALTER TABLE job ADD COLUMN {col} VARCHAR(40)")


def init_db() -> None:
    _migrate_arr_instance(engine)
    _migrate_job_node_id(engine)
    _migrate_node_pair_columns(engine)
    _migrate_media_file_original_path(engine)
    _migrate_media_file_episode_columns(engine)
    _migrate_job_mirror_columns(engine)
    _migrate_job_codec_columns(engine)
    Base.metadata.create_all(engine)
    # Seed runtime settings (api_key, auth defaults, delete_originals).
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
