from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..config import Policy
from ..workflows import WorkflowMatch

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
    # Per-file target codecs. Set by `evaluate()` based on which workflow (if
    # any) matched; falls back to the policy's defaults. `build_ffmpeg_args`
    # uses these to pick the right encoder family — without them, every file
    # would still go to HEVC regardless of what the user's workflows say.
    video_target_codec: str = "hevc"
    audio_target_codec: str = "aac"
    matched_workflow_id: int | None = None
    matched_workflow_name: str | None = None


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


def evaluate(probe: dict, policy: Policy, workflow: WorkflowMatch | None = None) -> FilePlan:
    """Apply a workflow to ffprobe output -> per-stream actions.

    Workflows are now the *only* trigger for conversion. When no workflow is
    supplied (no user rule matched, or the user has none defined), every
    stream is marked `copy` and `needs_conversion=False` — i.e. the rescan
    button is a no-op. The legacy "fall back to the global allowlist" path
    was surprising: users would click rescan with zero workflows configured
    and end up with a queue full of jobs they never asked for.

    When `workflow` IS supplied, its `video_codec` / `audio_codec` targets
    drive the per-stream decision: re-encode unless the source already
    matches the target (with codec aliasing — h265/x265/hevc are equivalent),
    or the workflow chose `copy` for that track type.
    """
    streams = probe.get("streams", [])
    type_idx = _stream_type_indices(streams)
    plan = FilePlan()

    if workflow is None:
        # No workflow → no-op plan. We still walk the streams so callers can
        # introspect the file (codecs, channels, etc.), but every action is
        # `copy` and `needs_conversion` stays False so no Job gets queued.
        plan.video_target_codec = "copy"
        plan.audio_target_codec = "copy"
        for s in streams:
            idx = s["index"]
            ti = type_idx[idx]
            ctype = s.get("codec_type", "")
            disposition = s.get("disposition", {}) or {}
            attached_pic = bool(disposition.get("attached_pic"))
            plan.streams.append(StreamPlan(
                index=idx,
                type_index=ti,
                codec_type=ctype,
                codec_name=(s.get("codec_name") or "").lower() or None,
                action="copy",
                reason="no workflow matched",
                channels=s.get("channels"),
                sample_rate=int(s["sample_rate"]) if s.get("sample_rate") else None,
                pix_fmt=s.get("pix_fmt"),
                color_space=s.get("color_space"),
                color_primaries=s.get("color_primaries"),
                color_trc=s.get("color_transfer"),
                width=s.get("width"),
                height=s.get("height"),
                is_attached_pic=attached_pic,
            ))
        return plan

    plan.video_target_codec = workflow.video_codec
    plan.audio_target_codec = workflow.audio_codec
    plan.matched_workflow_id = workflow.workflow_id
    plan.matched_workflow_name = workflow.workflow_name

    wf_video = workflow.video_codec
    wf_audio = workflow.audio_codec

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
            if wf_video == "copy":
                plan.streams.append(StreamPlan(action="copy", reason="workflow copy", **common))
            elif codec == wf_video or _codecs_equivalent(codec, wf_video):
                plan.streams.append(StreamPlan(
                    action="copy", reason=f"already {wf_video} (workflow)", **common,
                ))
            else:
                reason = f"workflow target {wf_video}, source {codec}"
                plan.streams.append(StreamPlan(action="reencode", reason=reason, **common))
                plan.needs_conversion = True
                plan.reasons.append(f"v:{ti} {reason}")
        elif ctype == "audio":
            if wf_audio == "copy":
                plan.streams.append(StreamPlan(action="copy", reason="workflow copy", **common))
            elif codec == wf_audio:
                plan.streams.append(StreamPlan(
                    action="copy", reason=f"already {wf_audio} (workflow)", **common,
                ))
            else:
                reason = f"workflow target {wf_audio}, source {codec}"
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


# Codec name equivalences. ffprobe reports canonical names ("hevc"/"avc") but
# users write workflows in the marketing names ("h265"/"h264"); without this
# map a "target hevc" workflow on an already-hevc file would re-encode it.
_CODEC_EQUIVALENTS: dict[str, set[str]] = {
    "hevc": {"hevc", "h265", "x265"},
    "h265": {"hevc", "h265", "x265"},
    "h264": {"avc", "h264", "x264"},
    "avc":  {"avc", "h264", "x264"},
    "av1":  {"av1"},
}


def _codecs_equivalent(a: str, b: str) -> bool:
    a, b = a.lower(), b.lower()
    if a == b:
        return True
    return b in _CODEC_EQUIVALENTS.get(a, set())
