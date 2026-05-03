"""Sonarr-style filter evaluation for the Series and Movies grids.

Two flavours:

  - **built-in filters** (key: "all", "monitored", "unmonitored", ...) are
    hard-coded predicates over the Sonarr/Radarr API response.

  - **custom filters** are stored in the `saved_filter` table and consist of a
    list of `{field, op, value}` clauses ANDed together. This module only does
    the evaluation; CRUD lives in `routes.py`.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def _series_has_file(item: dict) -> bool:
    return ((item.get("statistics") or {}).get("episodeFileCount") or 0) > 0


def _movie_has_file(item: dict) -> bool:
    return bool(item.get("hasFile"))


BUILTIN_FILTERS: dict[str, Callable[[dict, str], bool]] = {
    "all":          lambda item, kind: True,
    "monitored":    lambda item, kind: bool(item.get("monitored")),
    "unmonitored":  lambda item, kind: not item.get("monitored"),
    "continuing":   lambda item, kind: (item.get("status") or "").lower() == "continuing",
    "ended":        lambda item, kind: (item.get("status") or "").lower() == "ended",
    "has_file":     lambda item, kind: _series_has_file(item) if kind == "sonarr" else _movie_has_file(item),
    "missing":      lambda item, kind: not (_series_has_file(item) if kind == "sonarr" else _movie_has_file(item)),
}

BUILTIN_LABELS: dict[str, str] = {
    "all":          "All",
    "monitored":    "Monitored Only",
    "unmonitored":  "Unmonitored Only",
    "continuing":   "Continuing Only",
    "ended":        "Ended Only",
    "has_file":     "Has File",
    "missing":      "Missing",
}


# Fields the user can pick in the custom filter builder. The map's value is
# how to extract that field from a Sonarr/Radarr item (call signature: fn(item)).
def _ext_path(item: dict) -> str:
    return item.get("path") or ""


def _ext_genres(item: dict) -> list[str]:
    return list(item.get("genres") or [])


def _ext_tags(item: dict) -> list:
    return list(item.get("tags") or [])


def _extract_extension(path: str) -> str:
    """Last dotted suffix of a path's basename, lowercased. '' if none."""
    if not path:
        return ""
    base = path.rsplit("/", 1)[-1]
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def _ext_formats(item: dict) -> list[str]:
    """File extensions for this item — populated by the route handler before
    apply_filter runs (see routes.py series/movies flows). For movies we also
    fall back to deriving from `movieFile.path` so the field works even when
    the route enrichment is skipped (e.g. tests, edge cases)."""
    formats = item.get("_formats")
    if formats is not None:
        return list(formats)
    mf = item.get("movieFile") or {}
    ext = _extract_extension(mf.get("path", ""))
    return [ext] if ext else []


# Codec name aliases so users can type any of the common forms. ffprobe reports
# canonical names like "hevc" / "avc", but most people know these as the
# encoder names ("x264", "x265") or the marketing names ("h264", "h265").
_CODEC_ALIASES: dict[str, list[str]] = {
    "hevc": ["hevc", "h265", "x265"],
    "h265": ["hevc", "h265", "x265"],
    "x265": ["hevc", "h265", "x265"],
    "h264": ["avc", "h264", "x264"],
    "avc":  ["avc", "h264", "x264"],
    "x264": ["avc", "h264", "x264"],
    "vvc":  ["vvc", "h266", "x266"],
    "h266": ["vvc", "h266", "x266"],
    "x266": ["vvc", "h266", "x266"],
}


def expand_codec_aliases(codec: str) -> list[str]:
    """Return the canonical-plus-alias forms for a codec name."""
    if not codec:
        return []
    c = codec.lower()
    return _CODEC_ALIASES.get(c, [c])


def video_codecs_from_probe(probe: dict) -> list[str]:
    """Pull every video stream's codec_name out of an ffprobe blob, expanded
    with common aliases (hevc/h265, avc/h264). Excludes attached-picture
    streams (album art / posters embedded in mkv)."""
    if not probe:
        return []
    out: set[str] = set()
    for s in probe.get("streams") or []:
        if s.get("codec_type") != "video":
            continue
        if (s.get("disposition") or {}).get("attached_pic"):
            continue
        for alias in expand_codec_aliases(s.get("codec_name") or ""):
            out.add(alias)
    return sorted(out)


# Codec markers Radarr/Sonarr might cram into mediaInfo.videoCodec — those
# fields are free-form strings ("MPEG-4 Visual", "x264", "VC-1", "AV1") so we
# strip noise and match a known marker instead of doing a strict equality.
_KNOWN_CODEC_MARKERS = (
    "hevc", "h265", "x265", "h264", "x264", "avc", "av1", "vp9", "vp8",
    "vc1", "mpeg4", "mpeg2", "vvc", "h266", "x266",
)


def codecs_from_arr_mediainfo(value: str | None) -> list[str]:
    """Best-effort parse of Radarr/Sonarr's `mediaInfo.videoCodec` string into
    canonical codec names (with aliases)."""
    if not value:
        return []
    norm = value.lower().replace("-", "").replace(" ", "").replace("_", "")
    for marker in _KNOWN_CODEC_MARKERS:
        if marker in norm:
            return expand_codec_aliases(marker)
    return [norm]


def _ext_video_codecs(item: dict) -> list[str]:
    """Video codecs for this item — populated by the route handler from
    MediaFile.probe_json. Returns [] when not enriched."""
    codecs = item.get("_video_codecs")
    return list(codecs) if codecs is not None else []


CUSTOM_FIELDS: dict[str, dict] = {
    "title":            {"label": "Title",           "extract": lambda i: i.get("title", ""),     "type": "string"},
    "path":             {"label": "Path",            "extract": _ext_path,                          "type": "string"},
    "year":             {"label": "Year",            "extract": lambda i: i.get("year") or 0,     "type": "number"},
    "monitored":        {"label": "Monitored",       "extract": lambda i: bool(i.get("monitored")),"type": "bool"},
    "status":           {"label": "Status",          "extract": lambda i: i.get("status", ""),    "type": "string"},
    "network":          {"label": "Network",         "extract": lambda i: i.get("network", ""),   "type": "string"},
    "studio":           {"label": "Studio",          "extract": lambda i: i.get("studio", ""),    "type": "string"},
    "qualityProfileId": {"label": "Quality Profile", "extract": lambda i: i.get("qualityProfileId") or 0, "type": "number"},
    "tags":             {"label": "Tags",            "extract": _ext_tags,                          "type": "list"},
    "genres":           {"label": "Genres",          "extract": _ext_genres,                        "type": "list"},
    "format":           {"label": "File Format",     "extract": _ext_formats,                       "type": "list",
                         "suggestions": ["mkv", "mp4", "avi", "mov", "m4v", "ts", "webm", "wmv", "flv", "mpg", "mpeg"]},
    "video_codec":      {"label": "Video Codec",     "extract": _ext_video_codecs,                  "type": "list",
                         "suggestions": ["h264", "x264", "hevc", "h265", "x265", "av1", "vp9", "vp8", "mpeg4", "mpeg2video", "vc1", "vvc", "h266"]},
    "hasFile":          {"label": "Has File",        "extract": lambda i: bool(i.get("hasFile")), "type": "bool"},
    "rootFolderPath":   {"label": "Root Folder Path","extract": lambda i: i.get("rootFolderPath", ""), "type": "string"},
}


CUSTOM_OPS: dict[str, dict] = {
    "equal":       {"label": "is",            "applies_to": ("string", "number", "bool", "list")},
    "notEqual":    {"label": "is not",        "applies_to": ("string", "number", "bool", "list")},
    "contains":    {"label": "contains",      "applies_to": ("string", "list")},
    "notContains": {"label": "does not contain","applies_to": ("string", "list")},
    "greater":     {"label": "is greater than","applies_to": ("number",)},
    "less":        {"label": "is less than",  "applies_to": ("number",)},
}


def _coerce(value: str, kind: str) -> Any:
    """Coerce the raw form value to a comparable Python type."""
    if kind == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if kind == "bool":
        return str(value).lower() in {"true", "1", "yes", "on"}
    return str(value or "")


def evaluate_clause(item: dict, clause: dict) -> bool:
    field = CUSTOM_FIELDS.get(clause.get("field", ""))
    op = clause.get("op", "equal")
    if field is None:
        return True  # unknown field — don't filter out
    actual = field["extract"](item)
    target = _coerce(clause.get("value", ""), field["type"])

    if field["type"] == "list":
        # value-on-list semantics: "contains" => target in actual
        a_lower = [str(x).lower() for x in actual]
        t = str(target).lower()
        if op == "contains":
            return any(t in s for s in a_lower)
        if op == "notContains":
            return not any(t in s for s in a_lower)
        if op == "equal":
            return t in a_lower
        if op == "notEqual":
            return t not in a_lower
        return True

    if field["type"] == "string":
        a = str(actual or "").lower()
        t = str(target or "").lower()
        if op == "equal":       return a == t
        if op == "notEqual":    return a != t
        if op == "contains":    return t in a
        if op == "notContains": return t not in a
        return True

    if field["type"] == "number":
        a = float(actual or 0)
        t = float(target or 0)
        if op == "equal":       return a == t
        if op == "notEqual":    return a != t
        if op == "greater":     return a > t
        if op == "less":        return a < t
        return True

    if field["type"] == "bool":
        a = bool(actual)
        t = bool(target)
        if op == "equal":    return a == t
        if op == "notEqual": return a != t
        return True

    return True


def evaluate_custom(item: dict, clauses: list[dict]) -> bool:
    """All clauses ANDed together. No clauses = match everything."""
    return all(evaluate_clause(item, c) for c in (clauses or []))


def apply_filter(items: list[dict], filter_spec: dict | None, kind: str) -> list[dict]:
    """`filter_spec` is one of:
       - None or {"kind": "builtin", "name": "all"}     => everything
       - {"kind": "builtin", "name": "monitored"}        => predicate
       - {"kind": "custom",  "clauses": [...]}           => clause list
    """
    if not filter_spec:
        return items
    kind_spec = filter_spec.get("kind")
    if kind_spec == "builtin":
        pred = BUILTIN_FILTERS.get(filter_spec.get("name", "all"), BUILTIN_FILTERS["all"])
        return [it for it in items if pred(it, kind)]
    if kind_spec == "custom":
        clauses = filter_spec.get("clauses") or []
        return [it for it in items if evaluate_custom(it, clauses)]
    return items


def parse_filter_param(raw: str | None) -> dict | None:
    """Decode the `?filter=` query parameter.

    Format:
      - "all" or any builtin key -> {"kind": "builtin", "name": "<key>"}
      - "custom-{id}"            -> {"kind": "custom_id", "id": int}
                                      (caller resolves id -> clauses from DB)
    """
    if not raw:
        return None
    if raw.startswith("custom-"):
        try:
            return {"kind": "custom_id", "id": int(raw.split("-", 1)[1])}
        except ValueError:
            return None
    if raw in BUILTIN_FILTERS:
        return {"kind": "builtin", "name": raw}
    return None
