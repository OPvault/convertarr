"""Shared codec display helpers — used by both the statistics page and the
dashboard's running-job chips. Lives outside `web/` so backend modules
(e.g. workers/execution.py) can import without dragging the FastAPI stack.
"""
from __future__ import annotations


# ffprobe gives us aliases (h265/x265/hevc, h264/x264/avc, …); we collapse
# them to a single human label so a chip reads cleanly. Mirrors the alias
# logic in web/filters.py but keyed for display rather than matching.
_CODEC_CANONICAL: dict[str, str] = {
    "hevc": "HEVC (H.265)", "h265": "HEVC (H.265)", "x265": "HEVC (H.265)",
    "avc":  "H.264",         "h264": "H.264",         "x264": "H.264",
    "vvc":  "VVC (H.266)",   "h266": "VVC (H.266)",   "x266": "VVC (H.266)",
    "av1":  "AV1",
    "vp9":  "VP9",
    "vp8":  "VP8",
    "mpeg4": "MPEG-4",
    "mpeg2video": "MPEG-2",
    "mpeg2": "MPEG-2",
    "vc1":  "VC-1",
}


def canonical_codec(codec: str | None) -> str:
    """Pretty-print a codec name. Unknown codecs are upper-cased so they
    still look like a label rather than a raw ffmpeg token."""
    if not codec:
        return "Unknown"
    return _CODEC_CANONICAL.get(codec.lower(), codec.upper())


def format_conversion(src: str | None, dst: str | None) -> str | None:
    """'AV1 → HEVC (H.265)' when src and dst differ, else None.

    Returns None when either side is missing OR when the values are the
    same codec (no real conversion to advertise — happens when a workflow
    targets a codec the source already uses, leaving only a container
    remux). The caller treats None as 'don't render the chip'.
    """
    if not src or not dst:
        return None
    s = src.strip().lower()
    d = dst.strip().lower()
    if not s or not d or s == d:
        return None
    return f"{canonical_codec(s)} → {canonical_codec(d)}"
