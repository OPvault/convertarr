from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ArrKind(str, enum.Enum):
    sonarr = "sonarr"
    radarr = "radarr"


class JobState(str, enum.Enum):
    queued = "queued"
    running = "running"
    cancelling = "cancelling"
    cancelled = "cancelled"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class ArrInstance(Base):
    __tablename__ = "arr_instance"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ArrKind] = mapped_column(Enum(ArrKind))
    name: Mapped[str] = mapped_column(String(120))
    api_key: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # Bazarr-style fields. Replaces the older single `base_url` column;
    # the migration in db.py backfills these from the legacy value on first run.
    address: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)
    base_path: Mapped[str] = mapped_column(String(120), default="/")
    use_ssl: Mapped[bool] = mapped_column(Boolean, default=False)
    http_timeout: Mapped[int] = mapped_column(Integer, default=60)

    path_mappings: Mapped[list["PathMapping"]] = relationship(
        back_populates="arr_instance",
        cascade="all,delete-orphan",
        order_by="PathMapping.remote_path",
    )

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_ssl else "http"
        path = self.base_path.rstrip("/") if self.base_path and self.base_path != "/" else ""
        return f"{scheme}://{self.address}:{self.port}{path}"


class Setting(Base):
    """Key/value store for runtime-mutable settings (bind address, auth credentials,
    API key, conversion behavior toggles). Values are JSON-encoded so booleans,
    ints, and strings round-trip cleanly."""

    __tablename__ = "setting"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # JSON-encoded
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ImageCache(Base):
    """Cached poster bytes keyed by the resolved fetch URL.

    The first request for a given URL fetches and stores; subsequent requests
    are served straight from SQLite. Posters rarely change, so we cache without
    a TTL — flush manually if a cover ever updates.
    """

    __tablename__ = "image_cache"

    url: Mapped[str] = mapped_column(String(2000), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(100))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SavedFilter(Base):
    """User-built filter for the series/movies grid.

    `clauses` is a list of {field, op, value} dicts ANDed together. Evaluated
    server-side by `convertarr.web.filters.evaluate_clause`.
    """

    __tablename__ = "saved_filter"

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(20))  # "series" | "movies"
    name: Mapped[str] = mapped_column(String(120))
    clauses: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PathMapping(Base):
    """Translates a path the *arr returns into one Convertarr can read.

    Example (Radarr in docker, Convertarr on host):
        remote_path = "/movies/"
        local_path  = "/mnt/smbshare/Media/Movies/"
    Longest matching prefix wins.
    """

    __tablename__ = "path_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    arr_instance_id: Mapped[int] = mapped_column(ForeignKey("arr_instance.id"))
    remote_path: Mapped[str] = mapped_column(String(2000))
    local_path: Mapped[str] = mapped_column(String(2000))

    arr_instance: Mapped[ArrInstance] = relationship(back_populates="path_mappings")


class MediaFile(Base):
    __tablename__ = "media_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str] = mapped_column(String(2000), unique=True, index=True)
    arr_instance_id: Mapped[int | None] = mapped_column(ForeignKey("arr_instance.id"))
    arr_kind: Mapped[ArrKind | None] = mapped_column(Enum(ArrKind), nullable=True)
    arr_entity_id: Mapped[int | None] = mapped_column(Integer)  # seriesId or movieId
    arr_entity_title: Mapped[str | None] = mapped_column(String(500))

    size_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None]
    probe_json: Mapped[dict | None] = mapped_column(JSON)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    needs_conversion: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str | None] = mapped_column(Text)

    jobs: Mapped[list["Job"]] = relationship(back_populates="media_file", cascade="all,delete-orphan")


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    media_file_id: Mapped[int] = mapped_column(ForeignKey("media_file.id"))
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.queued, index=True)

    output_path: Mapped[str | None] = mapped_column(String(2000))
    encoder: Mapped[str | None] = mapped_column(String(60))
    ffmpeg_args: Mapped[list | None] = mapped_column(JSON)

    progress_pct: Mapped[float] = mapped_column(default=0.0)
    progress_speed: Mapped[float | None]
    progress_fps: Mapped[float | None]

    log_path: Mapped[str | None] = mapped_column(String(2000))
    error_tail: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    media_file: Mapped[MediaFile] = relationship(back_populates="jobs")
