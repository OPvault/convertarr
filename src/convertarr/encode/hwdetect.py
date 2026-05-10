from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

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


# Hardware/ffmpeg probes are memoized for the process lifetime — subprocess
# calls add hundreds of ms each and the results don't change without a
# restart. Detection runs on every page render via _ctx(); without this the
# Series/Movies pages spent most of their wall time here.
@lru_cache(maxsize=1)
def _ffmpeg_encoders_blob() -> str:
    try:
        out = subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return out.stdout


def _ffmpeg_has_encoder(name: str) -> bool:
    return any(name in line for line in _ffmpeg_encoders_blob().splitlines())


@lru_cache(maxsize=1)
def _has_nvidia_gpu() -> bool:
    if not shutil.which("nvidia-smi"):
        return False
    try:
        out = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and "GPU" in out.stdout


@lru_cache(maxsize=1)
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


# ---- Per-target-codec encoder picker (used by workflows) ----
#
# When a workflow targets a non-HEVC codec ("convert AV1 -> H.264"), the
# globally-detected HEVC encoder isn't appropriate — we need to pick the
# matching encoder in the same hardware family. The map below covers the
# combinations Convertarr actually supports; misses fall back to a CPU
# encoder so the encode never fails just because we couldn't find a hwencoder.

_CODEC_ENCODER_MAP: dict[tuple[str, str], EncoderProfile] = {
    # (target_codec, family) -> encoder
    ("hevc", "nvenc"): NVENC_HEVC,
    ("hevc", "vaapi"): VAAPI_HEVC,
    ("hevc", "qsv"):   QSV_HEVC,
    ("hevc", "amf"):   AMF_HEVC,
    ("hevc", "cpu"):   CPU_HEVC,

    ("h264", "nvenc"): EncoderProfile("h264_nvenc", "nvenc", "NVIDIA NVENC (h264_nvenc)"),
    ("h264", "vaapi"): EncoderProfile("h264_vaapi", "vaapi", "AMD/Intel VAAPI (h264_vaapi)"),
    ("h264", "qsv"):   EncoderProfile("h264_qsv",   "qsv",   "Intel QSV (h264_qsv)"),
    ("h264", "amf"):   EncoderProfile("h264_amf",   "amf",   "AMD AMF (h264_amf)"),
    ("h264", "cpu"):   EncoderProfile("libx264",    "cpu",   "CPU (libx264)"),

    # AV1 encoders are newer; libsvtav1 is the practical CPU choice.
    ("av1",  "nvenc"): EncoderProfile("av1_nvenc",  "nvenc", "NVIDIA NVENC (av1_nvenc)"),
    ("av1",  "vaapi"): EncoderProfile("av1_vaapi",  "vaapi", "AMD/Intel VAAPI (av1_vaapi)"),
    ("av1",  "qsv"):   EncoderProfile("av1_qsv",    "qsv",   "Intel QSV (av1_qsv)"),
    ("av1",  "cpu"):   EncoderProfile("libsvtav1",  "cpu",   "CPU (libsvtav1)"),
}


def encoder_for_codec(target_codec: str, *, base: EncoderProfile | None = None) -> EncoderProfile:
    """Pick the right encoder for a workflow's target codec, preserving the
    user's hardware-family choice. Falls back to the CPU encoder when there's
    no hwencoder in this family for that codec (e.g. AMF + AV1)."""
    base = base or detect_best()
    family = base.family if base.family in {"nvenc", "vaapi", "qsv", "amf", "cpu"} else "cpu"
    target = (target_codec or "hevc").lower()
    # Direct lookup; if the (target, family) pair isn't in the map (e.g. AMF
    # AV1, or anything paired with "override"/"manual"), fall back to CPU so
    # the encode still proceeds.
    enc = _CODEC_ENCODER_MAP.get((target, family))
    if enc is not None:
        return enc
    cpu_fallback = _CODEC_ENCODER_MAP.get((target, "cpu"))
    return cpu_fallback or base
