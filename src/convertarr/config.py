from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class VideoTarget(BaseModel):
    codec: str = "hevc"
    nvenc_cq: int = 22
    vaapi_qp: int = 22
    libx265_crf: int = 18
    nvenc_preset: str = "p6"
    libx265_preset: str = "slow"


class AudioTarget(BaseModel):
    codec: str = "aac"
    bitrate_per_channel_k: int = 96
    max_bitrate_k: int = 640


class Policy(BaseModel):
    video_allowlist: list[str] = ["h264", "hevc"]
    audio_allowlist: list[str] = ["aac", "ac3", "eac3", "mp3", "flac"]
    video_target: VideoTarget = VideoTarget()
    audio_target: AudioTarget = AudioTarget()
    container: str = "mkv"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONVERTARR_", env_file=".env", extra="ignore")

    project_root: Path = Path(__file__).resolve().parents[2]
    data_dir: Path = Path("data")
    output_dir: Path = Path("output")
    db_url: str = "sqlite:///data/convertarr.db"

    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    worker_poll_seconds: int = 10
    encoder_override: str | None = None  # e.g. "libx265" to force CPU

    policy: Policy = Policy()

    @property
    def absolute_data_dir(self) -> Path:
        p = self.data_dir if self.data_dir.is_absolute() else self.project_root / self.data_dir
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def absolute_output_dir(self) -> Path:
        p = self.output_dir if self.output_dir.is_absolute() else self.project_root / self.output_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
