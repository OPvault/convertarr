from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..config import settings


class ProbeError(RuntimeError):
    pass


async def ffprobe(path: str | Path) -> dict:
    proc = await asyncio.create_subprocess_exec(
        settings.ffprobe_bin,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed for {path}: {stderr.decode(errors='replace')}")
    return json.loads(stdout.decode())
