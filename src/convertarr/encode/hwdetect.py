from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from ..config import settings


@dataclass(frozen=True)
class EncoderProfile:
    name: str          # ffmpeg encoder name, e.g. "hevc_nvenc"
    family: str        # "nvenc" | "vaapi" | "amf" | "cpu"
    label: str         # human-readable


CPU_HEVC = EncoderProfile("libx265", "cpu", "CPU (libx265)")
NVENC_HEVC = EncoderProfile("hevc_nvenc", "nvenc", "NVIDIA NVENC (hevc_nvenc)")
VAAPI_HEVC = EncoderProfile("hevc_vaapi", "vaapi", "AMD/Intel VAAPI (hevc_vaapi)")
AMF_HEVC = EncoderProfile("hevc_amf", "amf", "AMD AMF (hevc_amf)")
QSV_HEVC = EncoderProfile("hevc_qsv", "qsv", "Intel QSV (hevc_qsv)")

ALL_HEVC: tuple[EncoderProfile, ...] = (NVENC_HEVC, VAAPI_HEVC, QSV_HEVC, AMF_HEVC, CPU_HEVC)


def _ffmpeg_has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return any(name in line for line in out.stdout.splitlines())


def _has_nvidia_gpu() -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and "GPU" in out.stdout


def _has_vaapi_device() -> bool:
    import os

    return any(os.path.exists(f"/dev/dri/renderD{n}") for n in range(128, 132))


def _auto_detect() -> EncoderProfile:
    if _has_nvidia_gpu() and _ffmpeg_has_encoder("hevc_nvenc"):
        return NVENC_HEVC
    if _has_vaapi_device() and _ffmpeg_has_encoder("hevc_vaapi"):
        return VAAPI_HEVC
    if _ffmpeg_has_encoder("hevc_qsv"):
        return QSV_HEVC
    if _ffmpeg_has_encoder("hevc_amf"):
        return AMF_HEVC
    return CPU_HEVC


def detect_best() -> EncoderProfile:
    """Return the encoder to use: env override > user setting > auto-detect."""
    if settings.encoder_override:
        return EncoderProfile(settings.encoder_override, "override", f"override ({settings.encoder_override})")
    try:
        from ..web import runtime_settings as rs
        choice = rs.get("encoder_choice", "auto")
    except Exception:
        choice = "auto"
    if choice and choice != "auto":
        for p in ALL_HEVC:
            if p.name == choice:
                return p
        return EncoderProfile(choice, "manual", f"manual ({choice})")
    return _auto_detect()


def is_detected(profile: EncoderProfile) -> bool:
    """True when the given encoder is usable on this host (hw + ffmpeg build)."""
    if profile.family == "nvenc":
        return _has_nvidia_gpu() and _ffmpeg_has_encoder("hevc_nvenc")
    if profile.family == "vaapi":
        return _has_vaapi_device() and _ffmpeg_has_encoder("hevc_vaapi")
    if profile.family in ("amf", "qsv", "cpu"):
        return _ffmpeg_has_encoder(profile.name)
    return False


def list_known() -> list[EncoderProfile]:
    """Every HEVC encoder Convertarr knows about, regardless of host support."""
    return list(ALL_HEVC)


def list_available() -> list[EncoderProfile]:
    """Encoders detected as usable on this host (legacy callers)."""
    return [p for p in ALL_HEVC if is_detected(p)]
