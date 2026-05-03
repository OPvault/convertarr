from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Mapping:
    remote: str
    local: str


def _norm(p: str) -> str:
    """Normalize a mapping prefix: strip trailing slashes, keep leading."""
    return p.rstrip("/\\")


def translate(path: str, mappings: Iterable[Mapping]) -> str:
    """Apply the longest-matching remote→local prefix mapping to `path`.

    If no mapping matches, returns `path` unchanged — appropriate when
    Convertarr is mounted with the same paths as the *arr.
    """
    best: Mapping | None = None
    best_len = -1
    for m in mappings:
        prefix = _norm(m.remote)
        # Match either exact or with a separator boundary so /movie doesn't
        # accidentally match /movies.
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "\\"):
            if len(prefix) > best_len:
                best = m
                best_len = len(prefix)
    if best is None:
        return path
    suffix = path[len(_norm(best.remote)):]
    return _norm(best.local) + suffix
