"""Workflow matching — figure out which user-defined rule applies to a file.

Workflows let users override the global allowlist policy with per-file rules
("if AV1, re-encode to HEVC", "if 4K, re-encode to H.264", ...). The matcher
distills an ffprobe blob down to the few attributes a user can write rules
against (codec, container, resolution, audio info), then walks the user's
workflows in priority order and returns the first match.

Returns a `WorkflowMatch` (the chosen target codecs + which workflow matched)
or `None` when no workflow matches and the allowlist should take over.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select

from .db import session_scope
from .models import Workflow

log = logging.getLogger(__name__)


VIDEO_CODEC_TARGETS = ("copy", "h264", "hevc", "av1")
AUDIO_CODEC_TARGETS = ("copy", "aac", "ac3", "eac3", "opus", "flac")


@dataclass(frozen=True)
class WorkflowMatch:
    workflow_id: int
    workflow_name: str
    video_codec: str  # one of VIDEO_CODEC_TARGETS
    audio_codec: str  # one of AUDIO_CODEC_TARGETS


# Field names exposed in the workflow condition builder, kept in sync with
# `evaluate_clause()` below. Keeping this as a flat dict makes it cheap to
# render the dropdowns in the UI.
WORKFLOW_FIELDS: dict[str, dict] = {
    "video_codec":    {"label": "Video Codec",       "type": "string",
                       "suggestions": ["h264", "hevc", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"]},
    "container":      {"label": "Container",         "type": "string",
                       "suggestions": ["mkv", "mp4", "avi", "mov", "ts", "webm"]},
    "resolution":     {"label": "Video Height (px)", "type": "number"},
    "audio_codec":    {"label": "Audio Codec",       "type": "string",
                       "suggestions": ["aac", "ac3", "eac3", "dts", "truehd", "flac", "opus", "mp3"]},
    "audio_channels": {"label": "Audio Channels",    "type": "number"},
}


WORKFLOW_OPS: dict[str, dict] = {
    "equal":       {"label": "is",            "applies_to": ("string", "number")},
    "notEqual":    {"label": "is not",        "applies_to": ("string", "number")},
    "contains":    {"label": "contains",      "applies_to": ("string",)},
    "notContains": {"label": "does not contain", "applies_to": ("string",)},
    "greater":     {"label": "is greater than", "applies_to": ("number",)},
    "less":        {"label": "is less than",  "applies_to": ("number",)},
}


def _primary_video_stream(probe: dict) -> dict | None:
    """First non-attached-pic video stream — the one we'd be re-encoding.
    Cover art / poster jpegs are dispositioned `attached_pic`, those don't
    count as the file's "real" video."""
    for s in probe.get("streams") or []:
        if s.get("codec_type") != "video":
            continue
        if (s.get("disposition") or {}).get("attached_pic"):
            continue
        return s
    return None


def _audio_streams(probe: dict) -> list[dict]:
    return [s for s in probe.get("streams") or [] if s.get("codec_type") == "audio"]


def _container_from_probe(probe: dict, path: str | None = None) -> str:
    """Best-effort container/format hint. `format.format_name` from ffprobe is a
    comma-separated list ("matroska,webm"); fall back to the path's extension."""
    fmt = (probe.get("format") or {}).get("format_name") or ""
    primary = fmt.split(",", 1)[0].strip().lower()
    if primary == "matroska":
        return "mkv"
    if primary in {"mov", "mp4", "m4a"}:
        return "mp4"
    if primary:
        return primary
    if path:
        return Path(path).suffix.lstrip(".").lower()
    return ""


def file_attributes(probe: dict, path: str | None = None) -> dict[str, Any]:
    """Distill a probe dict into the flat attribute map workflow conditions
    are evaluated against. Returning a dict keeps the matcher pure-data and
    trivial to test."""
    v = _primary_video_stream(probe) or {}
    audio = _audio_streams(probe)

    audio_codecs = sorted({(a.get("codec_name") or "").lower() for a in audio if a.get("codec_name")})
    max_channels = max((int(a.get("channels") or 0) for a in audio), default=0)

    return {
        "video_codec":    (v.get("codec_name") or "").lower(),
        "container":      _container_from_probe(probe, path),
        "resolution":     int(v.get("height") or v.get("coded_height") or 0),
        # `audio_codec` is plural under the hood — `contains` walks the list.
        "audio_codec":    audio_codecs,
        "audio_channels": max_channels,
    }


def _normalize_targets(value: Any) -> list[str]:
    """Coerce a clause's `value` into a non-empty list of lowercase strings.
    Backward-compatible: old workflows stored a single string; new ones store
    a list. Empty values become an empty list (caller treats that as 'no
    constraint' so partially-built clauses don't filter every file out)."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value]
    else:
        s = str(value or "").strip()
        # Old workflows that stored values as a comma-joined string still
        # round-trip correctly here.
        items = [t.strip() for t in s.split(",")] if s else []
    return [t.lower() for t in items if t]


def _match_string_one(actual: Any, target_l: str, op: str) -> bool:
    if isinstance(actual, list):
        items = [str(a).lower() for a in actual]
        if op == "equal":       return target_l in items
        if op == "notEqual":    return target_l not in items
        if op == "contains":    return any(target_l in a for a in items)
        if op == "notContains": return not any(target_l in a for a in items)
        return True
    a = str(actual or "").lower()
    if op == "equal":       return a == target_l
    if op == "notEqual":    return a != target_l
    if op == "contains":    return target_l in a
    if op == "notContains": return target_l not in a
    return True


def _match_string(actual: Any, value: Any, op: str) -> bool:
    """Multi-value matching:
       - "is" / "contains"          → ANY target matches  (OR across values)
       - "is not" / "does not c."   → NO target matches   (file has none of them)

    Single-value (legacy) just becomes a one-element list, so behavior is
    identical when only one value is selected."""
    targets = _normalize_targets(value)
    if not targets:
        # No constraint configured (user added a clause but didn't pick any
        # values yet) — match by default rather than silently filtering
        # everything out.
        return True
    if op in ("equal", "contains"):
        return any(_match_string_one(actual, t, op) for t in targets)
    if op in ("notEqual", "notContains"):
        return all(_match_string_one(actual, t, op) for t in targets)
    return True


def _match_number(actual: Any, value: Any, op: str) -> bool:
    """Numbers stay scalar — multi-select makes no sense for height
    comparisons. Lists collapse to the first element so the data shape is
    still uniform across field types."""
    raw = value[0] if isinstance(value, list) and value else value
    try:
        target = float(raw or 0)
    except (TypeError, ValueError):
        target = 0.0
    try:
        a = float(actual or 0)
    except (TypeError, ValueError):
        a = 0.0
    if op == "equal":   return a == target
    if op == "notEqual":return a != target
    if op == "greater": return a > target
    if op == "less":    return a < target
    return True


def evaluate_clause(attrs: dict, clause: dict) -> bool:
    field = WORKFLOW_FIELDS.get(clause.get("field", ""))
    if field is None:
        # Unknown field — don't match (better to be conservative; users will
        # notice their workflow not firing rather than firing on the wrong file).
        return False
    op = clause.get("op", "equal")
    actual = attrs.get(clause["field"]) if "field" in clause else None
    if field["type"] == "number":
        return _match_number(actual, clause.get("value", ""), op)
    return _match_string(actual, clause.get("value", ""), op)


def matches(attrs: dict, conditions: Iterable[dict]) -> bool:
    """Evaluate the boolean expression formed by the condition list.

    Each clause has an optional `connector` ("and" / "or") joining it to the
    previous one — the first clause's connector is implicit ("if"). AND binds
    tighter than OR (standard precedence), so the list is split into AND
    groups separated by OR connectors and the workflow matches when *any*
    group's clauses all pass:

        A AND B OR C AND D   =>  (A AND B) OR (C AND D)

    Empty list matches everything (a workflow with no conditions is a
    catch-all by design)."""
    cond_list = list(conditions or [])
    if not cond_list:
        return True
    groups: list[list[dict]] = [[]]
    for i, c in enumerate(cond_list):
        connector = (c.get("connector") or "and").lower()
        if i > 0 and connector == "or":
            groups.append([])
        groups[-1].append(c)
    return any(all(evaluate_clause(attrs, c) for c in g) for g in groups)


def load_active_workflows() -> list[dict]:
    """Snapshot of enabled workflows ordered by priority. Snapshot rather than
    SQLAlchemy objects so callers can drop the session — workers especially
    match outside any DB context."""
    with session_scope() as s:
        rows = s.scalars(
            select(Workflow).where(Workflow.enabled.is_(True)).order_by(Workflow.priority, Workflow.id)
        ).all()
        return [
            {
                "id": w.id,
                "name": w.name,
                "priority": w.priority,
                "conditions": list(w.conditions or []),
                "target_video_codec": w.target_video_codec,
                "target_audio_codec": w.target_audio_codec,
            }
            for w in rows
        ]


def pick_workflow(probe: dict, workflows: list[dict] | None = None,
                  path: str | None = None) -> WorkflowMatch | None:
    """Walk workflows in priority order; return the first whose conditions
    match. `workflows` may be passed in to avoid the DB round-trip on hot
    paths (the worker pre-loads them once per ingest cycle)."""
    if workflows is None:
        workflows = load_active_workflows()
    if not workflows:
        return None
    attrs = file_attributes(probe or {}, path)
    for wf in workflows:
        if matches(attrs, wf.get("conditions") or []):
            return _wf_to_match(wf)
    return None


def pick_workflow_by_id(probe: dict, workflow_id: int,
                        path: str | None = None) -> WorkflowMatch | None:
    """Evaluate a single, explicitly-chosen workflow against `probe` and
    return a match if its conditions still pass. Used by the rescan UI when
    the user picks a workflow from the dropdown — we honor their choice but
    don't bypass the workflow's own conditions, so a "convert AV1 → HEVC"
    rule won't fire on an HEVC file even if the user picks it manually."""
    with session_scope() as s:
        w = s.get(Workflow, workflow_id)
        if w is None or not w.enabled:
            return None
        wf = {
            "id": w.id,
            "name": w.name,
            "conditions": list(w.conditions or []),
            "target_video_codec": w.target_video_codec,
            "target_audio_codec": w.target_audio_codec,
        }
    attrs = file_attributes(probe or {}, path)
    if not matches(attrs, wf["conditions"]):
        return None
    return _wf_to_match(wf)


def _wf_to_match(wf: dict) -> WorkflowMatch:
    return WorkflowMatch(
        workflow_id=wf["id"],
        workflow_name=wf["name"],
        video_codec=(wf.get("target_video_codec") or "hevc").lower(),
        audio_codec=(wf.get("target_audio_codec") or "aac").lower(),
    )
