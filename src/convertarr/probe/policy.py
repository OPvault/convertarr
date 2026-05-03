from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..config import Policy

Action = Literal["copy", "reencode", "skip"]


@dataclass
class StreamPlan:
    """Per-input-stream decision."""

    index: int                       # input stream index (e.g. 0, 1, 2 ...)
    type_index: int                  # index within its type (v:0, a:0, a:1 ...)
    codec_type: str                  # "video" | "audio" | "subtitle" | "attachment" | "data"
    codec_name: str | None
    action: Action
    reason: str = ""
    # source attributes useful when building ffmpeg args
    channels: int | None = None
    sample_rate: int | None = None
    pix_fmt: str | None = None
    color_space: str | None = None
    color_primaries: str | None = None
    color_trc: str | None = None
    width: int | None = None
    height: int | None = None
    is_attached_pic: bool = False


@dataclass
class FilePlan:
    streams: list[StreamPlan] = field(default_factory=list)
    needs_conversion: bool = False
    reasons: list[str] = field(default_factory=list)


def _stream_type_indices(streams: list[dict]) -> dict[int, int]:
    """Returns {global_index: type_index} for each stream."""
    counters: dict[str, int] = {}
    out: dict[int, int] = {}
    for s in streams:
        t = s.get("codec_type", "")
        n = counters.get(t, 0)
        out[s["index"]] = n
        counters[t] = n + 1
    return out


def evaluate(probe: dict, policy: Policy) -> FilePlan:
    """Apply codec allowlist to ffprobe output -> per-stream actions."""
    streams = probe.get("streams", [])
    type_idx = _stream_type_indices(streams)
    plan = FilePlan()

    video_allow = {c.lower() for c in policy.video_allowlist}
    audio_allow = {c.lower() for c in policy.audio_allowlist}

    for s in streams:
        idx = s["index"]
        ti = type_idx[idx]
        ctype = s.get("codec_type", "")
        codec = (s.get("codec_name") or "").lower()
        disposition = s.get("disposition", {}) or {}
        attached_pic = bool(disposition.get("attached_pic"))

        common = dict(
            index=idx,
            type_index=ti,
            codec_type=ctype,
            codec_name=codec or None,
            channels=s.get("channels"),
            sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
            pix_fmt=s.get("pix_fmt"),
            color_space=s.get("color_space"),
            color_primaries=s.get("color_primaries"),
            color_trc=s.get("color_transfer"),
            width=s.get("width"),
            height=s.get("height"),
            is_attached_pic=attached_pic,
        )

        if ctype == "video":
            if attached_pic:
                # cover art / thumbnails — always copy, never re-encode
                plan.streams.append(StreamPlan(action="copy", reason="attached pic", **common))
                continue
            if codec in video_allow:
                plan.streams.append(StreamPlan(action="copy", reason="video codec in allowlist", **common))
            else:
                reason = f"video codec '{codec}' not in allowlist"
                plan.streams.append(StreamPlan(action="reencode", reason=reason, **common))
                plan.needs_conversion = True
                plan.reasons.append(f"v:{ti} {reason}")
        elif ctype == "audio":
            if codec in audio_allow:
                plan.streams.append(StreamPlan(action="copy", reason="audio codec in allowlist", **common))
            else:
                reason = f"audio codec '{codec}' not in allowlist"
                plan.streams.append(StreamPlan(action="reencode", reason=reason, **common))
                plan.needs_conversion = True
                plan.reasons.append(f"a:{ti} {reason}")
        elif ctype == "subtitle":
            plan.streams.append(StreamPlan(action="copy", reason="subtitles always copied", **common))
        elif ctype == "attachment":
            plan.streams.append(StreamPlan(action="copy", reason="attachment always copied", **common))
        else:
            # data, unknown — copy by default; ffmpeg will warn if it can't
            plan.streams.append(StreamPlan(action="copy", reason=f"{ctype} default copy", **common))

    return plan
