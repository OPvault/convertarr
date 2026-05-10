from __future__ import annotations

from pathlib import Path

from ..config import Policy, settings
from ..probe.policy import FilePlan, StreamPlan
from .hwdetect import EncoderProfile, encoder_for_codec


def _audio_bitrate_k(channels: int | None, policy: Policy) -> int:
    ch = channels or 2
    target = policy.audio_target.bitrate_per_channel_k * ch
    return min(target, policy.audio_target.max_bitrate_k)


def _video_args(plan: StreamPlan, encoder: EncoderProfile, policy: Policy, *, full_gpu: bool = False) -> list[str]:
    """Args for a re-encoded video stream, scoped by `-c:v:N`. When `full_gpu`
    is true, frames stay in GPU memory and we skip explicit pix_fmt (which
    would force a CPU↔GPU roundtrip on hardware-decoded inputs)."""
    n = plan.type_index
    is_10bit = (plan.pix_fmt or "").endswith("10le") or (plan.pix_fmt or "") in {"p010le", "yuv420p10le", "yuv422p10le", "yuv444p10le"}

    if encoder.family == "nvenc":
        args = [
            f"-c:v:{n}", encoder.name,
            f"-preset:v:{n}", policy.video_target.nvenc_preset,
            f"-tune:v:{n}", "hq",
            f"-rc:v:{n}", "vbr",
            f"-cq:v:{n}", str(policy.video_target.nvenc_cq),
            f"-b:v:{n}", "0",
        ]
        # In full-GPU mode the cuda hwframes are already in nv12/p010 — setting
        # `-pix_fmt yuv420p` here would force ffmpeg to download to system memory.
        if not full_gpu:
            args += [f"-pix_fmt:v:{n}", "p010le" if is_10bit else "yuv420p"]
    elif encoder.family == "vaapi":
        # Note: VAAPI requires upload via -vf format=nv12,hwupload — handled separately
        # since filters are global. For MVP we emit codec args only and add the filter
        # at the global level.
        args = [
            f"-c:v:{n}", encoder.name,
            f"-qp:v:{n}", str(policy.video_target.vaapi_qp),
        ]
    elif encoder.family == "amf":
        args = [
            f"-c:v:{n}", encoder.name,
            f"-quality:v:{n}", "quality",
            f"-rc:v:{n}", "cqp",
            f"-qp_i:v:{n}", str(policy.video_target.vaapi_qp),
            f"-qp_p:v:{n}", str(policy.video_target.vaapi_qp),
            f"-pix_fmt:v:{n}", "p010le" if is_10bit else "yuv420p",
        ]
    else:  # cpu / libx265 / override
        args = [
            f"-c:v:{n}", encoder.name,
            f"-preset:v:{n}", policy.video_target.libx265_preset,
            f"-crf:v:{n}", str(policy.video_target.libx265_crf),
            f"-pix_fmt:v:{n}", "yuv420p10le" if is_10bit else "yuv420p",
        ]

    # Preserve color metadata (matters for HDR/wide-gamut sources, but always safe to copy).
    if plan.color_space:
        args += [f"-colorspace:v:{n}", plan.color_space]
    if plan.color_primaries:
        args += [f"-color_primaries:v:{n}", plan.color_primaries]
    if plan.color_trc:
        args += [f"-color_trc:v:{n}", plan.color_trc]

    return args


def _audio_args(plan: StreamPlan, policy: Policy, target_codec: str | None = None) -> list[str]:
    n = plan.type_index
    bitrate = _audio_bitrate_k(plan.channels, policy)
    codec = (target_codec or policy.audio_target.codec).lower()
    args = [f"-c:a:{n}", codec]
    # Lossless codecs (flac) ignore bitrate; everything else benefits from a
    # per-channel bitrate so 5.1 doesn't get stomped at 128k stereo.
    if codec not in {"flac", "alac", "copy"}:
        args += [f"-b:a:{n}", f"{bitrate}k"]
    return args


CONVERTARR_SUFFIX = ".CONVERTARR"


def output_path_for(input_path: str | Path, policy: Policy | None = None) -> Path:
    """Save next to the source as `name.CONVERTARR.ext`.

    Keeps the converted file in the same folder as the original so the user
    can compare them side-by-side without a separate output tree, and the
    source extension is preserved (so ffmpeg infers the same muxer). The
    `.CONVERTARR.` infix prevents overwriting the original.
    """
    p = Path(input_path)
    return p.parent / f"{p.stem}{CONVERTARR_SUFFIX}{p.suffix}"


_HW_DECODABLE_CODECS = {
    "vaapi": {"h264", "hevc", "av1", "vp9", "vp8", "mpeg2video", "mpeg4"},
    # AV1 NVDEC needs an Ada Lovelace GPU (RTX 4060+). On Turing/Ampere it
    # claims support but fails at runtime ("Your platform doesn't support
    # hardware accelerated AV1 decoding."), producing zero frames and a
    # silent encode failure. Conservative default: SW-decode AV1 then upload
    # to NVENC. Older codecs are universally NVDEC-supported.
    "cuda":  {"h264", "hevc", "vp9", "vp8", "mpeg2video", "mpeg4", "vc1"},
    "qsv":   {"h264", "hevc", "av1", "vp9", "mpeg2video"},
}


def _hwaccel_family_for(encoder: EncoderProfile) -> str | None:
    """ffmpeg `-hwaccel` family that matches an encoder's family. AMF on Linux
    has no clean hwaccel pathway, so we leave it on CPU decode."""
    return {"vaapi": "vaapi", "nvenc": "cuda", "qsv": "qsv"}.get(encoder.family)


def _can_hw_decode(file_plan: FilePlan, encoder: EncoderProfile) -> bool:
    """True when the source video codec is in the hardware decoder's known
    capability set for this encoder's family. Mismatched codecs fall back to
    the SW-decode + hwupload path so we never hand the GPU something it can't
    handle (which would fail the entire encode at startup)."""
    fam = _hwaccel_family_for(encoder)
    if fam is None:
        return False
    src_stream = next(
        (s for s in file_plan.streams
         if s.codec_type == "video" and s.action == "reencode" and not s.is_attached_pic),
        None,
    )
    if src_stream is None or not src_stream.codec_name:
        return False
    if src_stream.codec_name not in _HW_DECODABLE_CODECS.get(fam, set()):
        return False
    # 10-bit H.264 (Hi10P) is decoded by VAAPI's Hi profile only on a sliver
    # of GPUs; most Intel/AMD VAAPI implementations bail with "Failed setup
    # for format vaapi: hwaccel initialisation returned error", which then
    # fails the encode because we already committed to the full-GPU pipeline
    # (no SW fallback path past that point). Same story on QSV. HEVC 10-bit
    # is fine — Main10 is broadly supported. NVENC's NVDEC handles Hi10P on
    # Pascal+, so leave cuda alone.
    if fam in ("vaapi", "qsv") and src_stream.codec_name == "h264" and _is_10bit(src_stream.pix_fmt):
        return False
    return True


def _is_10bit(pix_fmt: str | None) -> bool:
    """ffmpeg pixel-format strings encode bit depth in their suffix
    (yuv420p10le, p010le, etc.). Treat anything advertising 10/12/16-bit as
    "high bit depth"."""
    if not pix_fmt:
        return False
    return any(tag in pix_fmt for tag in ("p10", "p12", "p16", "p010", "p012", "p016"))


def build_ffmpeg_args(
    file_plan: FilePlan,
    encoder: EncoderProfile,
    input_path: str | Path,
    output_path: str | Path,
    policy: Policy | None = None,
) -> list[str]:
    """Construct the full ffmpeg argv for this file, given per-stream actions.

    `encoder` is the host's auto-detected default. When `file_plan` carries a
    workflow-driven target codec (e.g. "h264" instead of the default "hevc"),
    we re-pick the encoder via `encoder_for_codec` so we land on the right
    family-specific binary (h264_vaapi, libx264, etc.) without the caller
    needing to know about workflows."""
    policy = policy or settings.policy
    if file_plan.video_target_codec and file_plan.video_target_codec != "copy":
        encoder = encoder_for_codec(file_plan.video_target_codec, base=encoder)
    args: list[str] = [settings.ffmpeg_bin, "-hide_banner", "-y"]

    has_video_reencode = any(
        s.codec_type == "video" and s.action == "reencode" and not s.is_attached_pic
        for s in file_plan.streams
    )
    full_gpu = has_video_reencode and _can_hw_decode(file_plan, encoder)

    # ---- Pre-input hwaccel setup ----
    # Full-GPU pipeline: decode + encode both happen on the GPU and frames
    # never leave VRAM. This is the big CPU win for h264/hevc/av1 sources —
    # without it, ffmpeg decodes on CPU then bounces every frame through
    # system memory before encoding (which is why CPU pegs at 100%).
    if has_video_reencode and encoder.family == "vaapi":
        args += ["-vaapi_device", "/dev/dri/renderD128"]
        if full_gpu:
            args += ["-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi"]
    elif full_gpu and encoder.family == "nvenc":
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    elif full_gpu and encoder.family == "qsv":
        args += ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv"]

    args += ["-i", str(input_path)]

    # Selective mapping: stream specifier `V` (uppercase) means "video that is
    # NOT an attached_pic", so cover.jpg-style thumbnails are dropped. We keep
    # them out of the mux because matroska treats them as attachment-like and
    # ffmpeg fails with "Received a packet for an attachment stream" when the
    # demuxer produces packets for them after the muxer has already finalized
    # the attachment header. The `?` suffix makes each map optional so ffmpeg
    # doesn't error out on files that lack a stream type.
    args += ["-map", "0:V?"]
    args += ["-map", "0:a?"]
    args += ["-map", "0:s?"]
    args += ["-map", "0:t?"]
    args += ["-map", "0:d?"]

    # If the source codec isn't in the hw-decode list (or the encoder family
    # has no hwaccel pathway, e.g. AMF), fall back to SW-decode + hwupload for
    # VAAPI. NVENC and QSV handle the upload internally when not in full-GPU
    # mode so they don't need a filter here.
    if encoder.family == "vaapi" and has_video_reencode and not full_gpu:
        target_idx = next(
            s.type_index for s in file_plan.streams
            if s.codec_type == "video" and s.action == "reencode" and not s.is_attached_pic
        )
        args += [f"-filter:v:{target_idx}", "format=nv12|vaapi,hwupload"]

    # Default copies for subtitle/attachment/data. Per-stream args below
    # override for video and audio.
    args += ["-c:s", "copy", "-c:t", "copy", "-c:d", "copy"]

    # Video streams. We skip attached_pic since `-map 0:V?` excluded them from
    # the output — emitting `-c:v:N copy` for a non-existent output stream
    # would error. Output type-indices for the remaining videos are dense
    # (0, 1, 2…) but since we currently only re-encode the first one and
    # multi-real-video files are exotic, the input type_index used here
    # matches the output type-index in practice.
    for s in file_plan.streams:
        if s.codec_type != "video":
            continue
        if s.is_attached_pic:
            continue
        if s.action == "copy":
            args += [f"-c:v:{s.type_index}", "copy"]
        else:
            args += _video_args(s, encoder, policy, full_gpu=full_gpu)

    # Audio streams. Use the plan's per-file target codec so workflow
    # decisions ("convert DTS to EAC3") flow through.
    for s in file_plan.streams:
        if s.codec_type != "audio":
            continue
        if s.action == "copy":
            args += [f"-c:a:{s.type_index}", "copy"]
        else:
            args += _audio_args(s, policy, target_codec=file_plan.audio_target_codec)

    # Preserve global metadata + chapters.
    args += ["-map_metadata", "0", "-map_chapters", "0"]

    # Progress reporting on stdout, suppress per-frame stats on stderr.
    # `-stats_period 0.5` forces ffmpeg to flush a progress block every 0.5 s
    # (default is 0.5s but explicit so it survives buffering changes).
    args += ["-progress", "pipe:1", "-nostats", "-stats_period", "0.5"]

    args.append(str(output_path))
    return args
