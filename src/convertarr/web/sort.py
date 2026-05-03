"""Sort options for the Series and Movies grids.

Mirrors the sort menu items shown by Sonarr (series) and Radarr (movies). No
custom sort builder — just these built-in fields. Each entry is `key →
{label, extract}` where `extract(item)` returns a comparable value.

Empty / missing values sort consistently to the end regardless of direction
by returning `(is_empty, value)` tuples — Python's stable sort preserves the
secondary order.
"""
from __future__ import annotations

from typing import Any


def _str(v: Any) -> str:
    return (v or "").lower() if isinstance(v, str) else ""


def _num(v: Any) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _date(v: Any) -> str:
    """ISO-8601 strings sort lexically. Empty -> empty string (handled separately)."""
    return v if isinstance(v, str) else ""


def _series_latest_season(i: dict) -> int:
    seasons = i.get("seasons") or []
    return max((s.get("seasonNumber") or 0 for s in seasons), default=0)


def _movie_release_date(i: dict) -> str:
    """Radarr's 'Release Date' = whichever of digital/physical/inCinemas is present last."""
    candidates = [i.get("digitalRelease"), i.get("physicalRelease"), i.get("inCinemas")]
    candidates = [c for c in candidates if c]
    return max(candidates) if candidates else ""


def _ratings_value(i: dict, key: str) -> float:
    ratings = i.get("ratings") or {}
    block = ratings.get(key) or {}
    if isinstance(block, dict):
        return _num(block.get("value"))
    return 0.0


SERIES_SORT: dict[str, dict] = {
    "monitoredStatus":  {"label": "Monitored/Status",  "extract": lambda i: (not bool(i.get("monitored")), _str(i.get("status")))},
    "title":            {"label": "Title",             "extract": lambda i: _str(i.get("sortTitle") or i.get("title"))},
    "network":          {"label": "Network",           "extract": lambda i: _str(i.get("network"))},
    "originalLanguage": {"label": "Original Language", "extract": lambda i: _str((i.get("originalLanguage") or {}).get("name"))},
    "qualityProfileId": {"label": "Quality Profile",   "extract": lambda i: _num(i.get("qualityProfileId"))},
    "nextAiring":       {"label": "Next Airing",       "extract": lambda i: _date(i.get("nextAiring"))},
    "previousAiring":   {"label": "Previous Airing",   "extract": lambda i: _date(i.get("previousAiring"))},
    "added":            {"label": "Added",             "extract": lambda i: _date(i.get("added"))},
    "seasonCount":      {"label": "Seasons",           "extract": lambda i: _num((i.get("statistics") or {}).get("seasonCount"))},
    "episodeProgress":  {"label": "Episodes",          "extract": lambda i: _num((i.get("statistics") or {}).get("episodeFileCount"))},
    "episodeCount":     {"label": "Episode Count",     "extract": lambda i: _num((i.get("statistics") or {}).get("episodeCount"))},
    "latestSeason":     {"label": "Latest Season",     "extract": _series_latest_season},
    "path":             {"label": "Path",              "extract": lambda i: _str(i.get("path"))},
    "sizeOnDisk":       {"label": "Size on disk",      "extract": lambda i: _num((i.get("statistics") or {}).get("sizeOnDisk"))},
    "tags":             {"label": "Tags",              "extract": lambda i: len(i.get("tags") or [])},
    "ratings":          {"label": "Rating",            "extract": lambda i: _ratings_value(i, "value") or _num((i.get("ratings") or {}).get("value"))},
}

MOVIES_SORT: dict[str, dict] = {
    "monitoredStatus":  {"label": "Monitored/Status",  "extract": lambda i: (not bool(i.get("monitored")), _str(i.get("status")))},
    "title":            {"label": "Title",             "extract": lambda i: _str(i.get("sortTitle") or i.get("title"))},
    "studio":           {"label": "Studio",            "extract": lambda i: _str(i.get("studio"))},
    "qualityProfileId": {"label": "Quality Profile",   "extract": lambda i: _num(i.get("qualityProfileId"))},
    "added":            {"label": "Added",             "extract": lambda i: _date(i.get("added"))},
    "year":             {"label": "Year",              "extract": lambda i: _num(i.get("year"))},
    "inCinemas":        {"label": "In Cinemas",        "extract": lambda i: _date(i.get("inCinemas"))},
    "digitalRelease":   {"label": "Digital Release",   "extract": lambda i: _date(i.get("digitalRelease"))},
    "physicalRelease":  {"label": "Physical Release",  "extract": lambda i: _date(i.get("physicalRelease"))},
    "releaseDate":      {"label": "Release Date",      "extract": _movie_release_date},
    "tmdbRating":       {"label": "TMDb Rating",       "extract": lambda i: _ratings_value(i, "tmdb")},
    "imdbRating":       {"label": "IMDb Rating",       "extract": lambda i: _ratings_value(i, "imdb")},
    "rottenTomatoesRating": {"label": "Tomato Rating", "extract": lambda i: _ratings_value(i, "rottenTomatoes")},
    "traktRating":      {"label": "Trakt Rating",      "extract": lambda i: _ratings_value(i, "trakt")},
    "popularity":       {"label": "Popularity",        "extract": lambda i: _num(i.get("popularity"))},
    "path":             {"label": "Path",              "extract": lambda i: _str(i.get("path"))},
    "sizeOnDisk":       {"label": "Size on Disk",      "extract": lambda i: _num(i.get("sizeOnDisk"))},
    "certification":    {"label": "Certification",     "extract": lambda i: _str(i.get("certification"))},
    "originalTitle":    {"label": "Original Title",    "extract": lambda i: _str(i.get("originalTitle"))},
    "originalLanguage": {"label": "Original Language", "extract": lambda i: _str((i.get("originalLanguage") or {}).get("name"))},
    "tags":             {"label": "Tags",              "extract": lambda i: len(i.get("tags") or [])},
}


DEFAULT_SORT = {"series": "title", "movies": "title"}
DEFAULT_DIR = "asc"


def options_for(scope: str) -> list[dict]:
    table = SERIES_SORT if scope == "series" else MOVIES_SORT
    return [{"key": k, "label": v["label"]} for k, v in table.items()]


def label_for(scope: str, key: str) -> str:
    table = SERIES_SORT if scope == "series" else MOVIES_SORT
    if key in table:
        return table[key]["label"]
    return table[DEFAULT_SORT[scope]]["label"]


def apply_sort(items: list[dict], key: str | None, direction: str | None, scope: str) -> list[dict]:
    table = SERIES_SORT if scope == "series" else MOVIES_SORT
    sort_key = key if key in table else DEFAULT_SORT[scope]
    reverse = (direction or DEFAULT_DIR) == "desc"
    extract = table[sort_key]["extract"]

    def _sort_key(item: dict):
        v = extract(item)
        # Treat empty strings, None, and 0 as "missing" so they sink to the end
        # regardless of direction (Sonarr-like behavior).
        if isinstance(v, tuple):
            return v
        is_empty = v is None or v == "" or v == 0
        # XOR with reverse: when reverse=True, the second element of the key
        # would naturally invert; multiplying empties through keeps them last.
        return (is_empty != reverse, v)

    return sorted(items, key=_sort_key, reverse=reverse)
