# Convertarr

A Sonarr-style daemon that pre-converts media into a Jellyfin direct-play codec set, so the server never has to transcode at playback time. Pulls file paths from Sonarr / Radarr, ffprobes them, and re-encodes only out-of-allowlist streams (everything else — subtitles, attachments, chapters, untouched audio tracks — is stream-copied bit-for-bit).

## Quick start

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e .

uvicorn convertarr.main:app --reload
```

Open <http://localhost:6565>, add your Sonarr + Radarr instances on the Settings page, and click Rescan on a series or movie.

## Docker

Prebuilt images are published to GHCR on every push to `main` and on every `v*` tag.

```sh
docker run -d \
    --name convertarr \
    -p 6565:6565 \
    -v $(pwd)/config:/config \
    -v /path/to/media:/path/to/media \
    --device /dev/dri:/dev/dri \
    ghcr.io/opvault/convertarr:latest
```

Or use the sample [`docker-compose.yml`](docker-compose.yml) — it has commented blocks for VAAPI (Intel/AMD) and NVENC (NVIDIA) hardware acceleration.

Bind-mount your library at the **same path** Sonarr/Radarr use, so paths Convertarr receives from \*arr resolve directly without per-instance translation.

## Default codec policy

| | Allowed (stream-copy) | Re-encoded |
|---|---|---|
| Video | h264, hevc | av1, vp9, mpeg2, etc. |
| Audio | aac, ac3, eac3, mp3, flac | opus, vorbis, dts, truehd |
| Subtitles | always copied | — |

Re-encode targets HEVC video (NVENC / VAAPI / libx265 — auto-detected) and AAC audio. Original 10-bit pixel format and color metadata are preserved.

## Testing phase

Outputs are written to `output/` mirroring the source tree. Originals are untouched. In-place replacement is intentionally disabled until the converter is verified.
