from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Enum, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
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


class Workflow(Base):
    """User-defined re-encode rule.

    A workflow says: "when a file matches these conditions, target this
    video/audio codec." The first matching enabled workflow (lowest priority
    number wins) overrides the global policy's video/audio target codec for
    that file. When no workflow matches the existing allowlist policy
    applies — so adding workflows is additive, never breaks the default flow.

    `conditions` is a list of {field, op, value} dicts ANDed together. Fields
    operate on the source's primary video stream + file extension:
      - video_codec   (string; eg "av1", "hevc", "h264")
      - container     (string; eg "mkv", "mp4")
      - resolution    (number; primary video stream height)
      - audio_codec   (string; eg "dts", "truehd", "aac" — matches any audio
                       stream when used)
      - audio_channels (number; max channels across audio streams)

    Targets:
      - video: "copy" | "h264" | "hevc" | "av1"
      - audio: "copy" | "aac"  | "ac3"  | "eac3" | "opus" | "flac"

    Picking "copy" means the workflow has matched but doesn't want to
    re-encode that track type — useful for "this is already fine, leave it".
    """

    __tablename__ = "workflow"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(default=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    target_video_codec: Mapped[str] = mapped_column(String(20), default="hevc")
    target_audio_codec: Mapped[str] = mapped_column(String(20), default="aac")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class EntityIndex(Base):
    """Pre-computed format + video-codec set for one Sonarr series or Radarr movie.

    The series/movies grids let users filter on `format` and `video_codec`, both
    of which require either a probe (MediaFile) or an N+1 fetch from the *arr
    (`/episodefile?seriesId=X` per series). The indexer worker populates this
    table in the background so the filter path is a single indexed lookup
    instead of hundreds of HTTP calls every page load.

    `formats` and `video_codecs` are sorted lowercase lists, ready to plug
    straight into `apply_filter` via the `_formats` / `_video_codecs` keys."""

    __tablename__ = "entity_index"
    __table_args__ = (
        UniqueConstraint("arr_kind", "arr_instance_id", "arr_entity_id", name="uq_entity_index_entity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    arr_kind: Mapped[ArrKind] = mapped_column(Enum(ArrKind), index=True)
    arr_instance_id: Mapped[int] = mapped_column(ForeignKey("arr_instance.id"), index=True)
    arr_entity_id: Mapped[int] = mapped_column(Integer, index=True)
    formats: Mapped[list] = mapped_column(JSON, default=list)
    video_codecs: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Node(Base):
    """A worker node — either the host's built-in worker (`is_local=True`,
    `id="local"`) or a remote `convertarr-worker` process.

    The string `id` is the stable, worker-generated UUID (or the literal
    "local" for the host). Remote workers persist this id locally and send
    it on every API call so the host can attribute claims/heartbeats/
    progress to the right node row across restarts.

    Encoder fields are populated on `register`: workers ship their detected
    `EncoderProfile` so the Nodes UI can show "this box has NVENC, that one
    has VAAPI" without the host having to run hardware detection on the
    worker's behalf. `encoder_choice` mirrors the global setting but per-
    node, so the user can pin a specific encoder on one box.
    """

    __tablename__ = "node"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    is_local: Mapped[bool] = mapped_column(Boolean, default=False)

    encoder_family: Mapped[str | None] = mapped_column(String(20))
    encoder_name: Mapped[str | None] = mapped_column(String(60))
    encoder_choice: Mapped[str] = mapped_column(String(60), default="auto")

    # Per-node concurrency cap. Read fresh by the worker each loop tick (via
    # heartbeat response) so the user can change it from the UI without a
    # restart on the worker side.
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, default=1)

    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_register: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    address: Mapped[str | None] = mapped_column(String(200))   # display only
    version: Mapped[str | None] = mapped_column(String(60))    # client version

    # Set on pair time (host UI form) so unpair can call the worker back
    # without re-prompting the operator. `pair_url` is the full URL the host
    # uses for callbacks; `pair_api_key` is the worker's own X-Api-Key.
    pair_url: Mapped[str | None] = mapped_column(String(255))
    pair_api_key: Mapped[str | None] = mapped_column(String(200))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    path_mappings: Mapped[list["NodePathMapping"]] = relationship(
        back_populates="node",
        cascade="all,delete-orphan",
        order_by="NodePathMapping.host_path",
    )


class NodePathMapping(Base):
    """Per-node path translation. Mirrors `PathMapping` (which translates
    arr→Convertarr paths), but here we translate from "the path the host
    stores in MediaFile.path" to "the path this worker sees on its own
    filesystem". The host applies these before dispatching a job so each
    worker receives paths it can actually open.

    Longest-prefix-match semantics handled by `arr/paths.py:translate`.
    """

    __tablename__ = "node_path_mapping"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("node.id"), index=True)
    host_path: Mapped[str] = mapped_column(String(2000))
    local_path: Mapped[str] = mapped_column(String(2000))

    node: Mapped[Node] = relationship(back_populates="path_mappings")


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
    # Path as the HOST sees it on its own filesystem — already translated
    # through this host's `PathMapping` rows during ingest. Used by the
    # local worker for ffmpeg.
    path: Mapped[str] = mapped_column(String(2000), unique=True, index=True)
    # Path as the *arr originally returned it (pre-translation). Stored so a
    # remote worker can apply its OWN arr `PathMapping` rows to derive its
    # local view — no need for a separate node-level mapping table.
    # Nullable for legacy rows ingested before this column existed; falls
    # back to `path` on dispatch.
    arr_original_path: Mapped[str | None] = mapped_column(String(2000))
    arr_instance_id: Mapped[int | None] = mapped_column(ForeignKey("arr_instance.id"))
    arr_kind: Mapped[ArrKind | None] = mapped_column(Enum(ArrKind), nullable=True)
    arr_entity_id: Mapped[int | None] = mapped_column(Integer)  # seriesId or movieId
    arr_entity_title: Mapped[str | None] = mapped_column(String(500))
    # Sonarr-only: filled in during ingest so the dashboard / queue can show
    # `Show - S01E03` instead of just the series name when many episodes of
    # the same show are encoding at once.
    season_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode_number: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    # Nullable so worker-side mirror rows (which have no local MediaFile)
    # can live in the same table.
    media_file_id: Mapped[int | None] = mapped_column(ForeignKey("media_file.id"), nullable=True)
    state: Mapped[JobState] = mapped_column(Enum(JobState), default=JobState.queued, index=True)

    # Set when a worker claims this job; cleared by the heartbeat watchdog
    # if the owning worker disappears (the job goes back to `queued` for a
    # healthy node to pick up). FK is intentionally nullable to keep
    # legacy rows valid through the migration.
    node_id: Mapped[str | None] = mapped_column(ForeignKey("node.id"), index=True, nullable=True)

    # On the host: null. On a worker: the host's Job.id this row mirrors.
    # Lets the worker render its own dashboard/history/statistics from the
    # same Job table without needing a MediaFile row, and lets the operator
    # tell mirror rows apart from local-mode rows on the worker UI.
    host_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Denormalized title for mirror rows (no MediaFile to read from). On
    # the host this is left null and the UI falls back to MediaFile.
    display_title: Mapped[str | None] = mapped_column(String(500))
    # The actual on-disk source path used by the node that ran the job.
    # On the host for remote jobs this is filled from the worker's report
    # so /history shows the worker-side path (which may differ from the
    # host's MediaFile.path under different mount layouts). For local jobs
    # the UI keeps falling back to MediaFile.path.
    source_path: Mapped[str | None] = mapped_column(String(2000))
    # Snapshot of Node.name at finish time so /history survives renames or
    # node deletion.
    node_name: Mapped[str | None] = mapped_column(String(120))

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

    media_file: Mapped[MediaFile | None] = relationship(back_populates="jobs")
