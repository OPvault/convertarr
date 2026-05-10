<p align="center">
  <img src="assets/icon.svg" alt="Convertarr" width="128">
</p>

<h1 align="center">Convertarr</h1>

The missing \*arr for your media server. Convertarr plugs into Sonarr and Radarr, ffprobes every file, and pre-encodes anything Jellyfin or Plex would otherwise have to transcode at playback — so streaming stays direct-play. Subtitles, attachments, chapters, and matching audio tracks are stream-copied bit-for-bit; only the offending streams get re-encoded.

## Features

- **Sonarr + Radarr integration** — point at your existing instances, click rescan, watch the queue fill.
- **Workflow-driven** — every conversion is triggered by a user-defined rule. Conditions on source codec, container, resolution, or audio channels select which files match; the workflow's video + audio codec targets drive the encode. No workflows defined = rescan is a no-op until you create one.
- **Hardware acceleration** — auto-detects NVENC (NVIDIA), VAAPI (Intel/AMD), QSV (Intel), AMF (AMD), or falls back to libx265 (CPU). Full-GPU pipeline keeps frames in VRAM end-to-end where supported.
- **Multi-node** — pair a second Convertarr instance as a worker. The host owns the queue; workers pull jobs over HTTP and run them on their own hardware. Each worker reports its own concurrency cap and encoder.
- **Live dashboard** — running jobs show progress, ETA, fps, speed, and a `AV1 → HEVC` codec chip so you can see exactly what's being transformed.
- **History + statistics** — `/system/statistics` charts codec, container, and resolution distribution across your library; `/system/about` shows version and uptime.
- **Backup / restore** — export every user-editable setting (instances, path mappings, workflows, saved filters) as a JSON, import it on a fresh install.

## Quick start (Docker)

Grab the sample [`docker-compose.yml`](docker-compose.yml) (NVIDIA-by-default; VAAPI / CPU notes inline at the bottom of the file), drop it next to a `config/` folder, and:

```sh
docker compose up -d
```

Adjust:

- **Volumes** — bind your media at the **same path** Sonarr/Radarr uses internally. Convertarr receives the *arr-relative path and opens it directly; mismatched paths force you to set up Path Mappings on every \*arr instance.
- **`PUID` / `PGID`** — set to the user that owns your media bind-mounts. `0/0` runs the container as root if you can't or won't chown.
- **GPU** — see "Hardware acceleration" below.

Open <http://localhost:6565>, add your Sonarr/Radarr instances under **Settings → Sonarr / Radarr**, define a workflow under **Settings → Workflows**, then click Rescan on a series or movie.

Prebuilt images are published to <https://github.com/OPvault/convertarr/pkgs/container/convertarr> on every push to `main` and every `v*` tag. Pin a specific release with `:1.0.1` etc.; `:latest` always tracks the most recent tag.

## Quick start (bare metal)

For development or environments where Docker isn't available. Requires Python 3.12+, ffmpeg, and ffprobe.

```sh
git clone https://github.com/OPvault/convertarr
cd convertarr
./run.sh
```

`run.sh` creates the venv on first run, installs the package in editable mode, and launches Convertarr on `0.0.0.0:6565`. Pass `--no-reload` for production-style runs.

State (DB, logs, runtime settings) lives in `data/` next to the source tree. Override with `CONVERTARR_DATA_DIR=/some/path`.

## Workflows

A workflow is a "**when** the source matches these conditions, **target** these codecs" rule. Workflows are the only path to a conversion — Convertarr runs nothing until at least one is defined. Build them under **Settings → Workflows**.

### What a workflow contains

- **Name** — anything; surfaces on the dashboard chip and in history.
- **Priority** — lower number wins. The first enabled workflow whose conditions match is chosen and the rest are skipped.
- **Conditions** — zero or more clauses ANDed/ORed together. An empty list is a catch-all.
- **Target video codec** — `copy`, `h264`, `hevc`, or `av1`.
- **Target audio codec** — `copy`, `aac`, `ac3`, `eac3`, `opus`, or `flac`.

### Condition fields

| Field | Type | Source | Notes |
|---|---|---|---|
| `video_codec` | string | primary video stream's `codec_name` | `attached_pic` (cover art) is ignored |
| `container` | string | ffprobe `format_name`, normalised | `matroska` → `mkv`, `mov,mp4,m4a` → `mp4` |
| `resolution` | number | primary video stream height | e.g. `1080`, `2160` |
| `audio_codec` | string | **set** of every audio stream's codec | `is` matches when any audio track is that codec |
| `audio_channels` | number | max channels across audio streams | a 5.1 + stereo file evaluates to `6` |

### Operators

- **`is` / `is not`** — exact match. Multi-value: `is` matches if any value matches; `is not` requires none of them match.
- **`contains` / `does not contain`** — substring (strings only). Useful for catching variants like `mpeg2video`.
- **`is greater than` / `is less than`** — numeric (height, channels).

Clauses are joined with `and` (default) or `or`. Standard precedence — `A and B or C and D` becomes `(A and B) or (C and D)`. A workflow matches when any AND-group passes.

### What runs after a match

The matched workflow's video / audio targets become the per-file plan. Convertarr then walks every stream and decides:

- **Video** (non-cover-art): re-encode if the source codec doesn't equal the target, copy otherwise. `h264` / `avc` / `x264` are treated as the same codec — same for `hevc` / `h265` / `x265` — so a "target hevc" rule on an already-hevc file is a no-op even if the source codec is named `x265`.
- **Audio**: re-encode the streams that aren't the target codec; the rest are stream-copied.
- **Subtitles, attachments (fonts), chapters, data**: always copied bit-for-bit.
- **Cover art / poster jpegs** (dispositioned `attached_pic`): always copied.

If every stream ends up `copy`, no Job is queued — the rescan is treated as "nothing to do" rather than firing a remux.

### Example

A real-world catch-all that normalises everything into HEVC video + AAC audio:

```json
{
  "name": "Normalize to HEVC + AAC",
  "enabled": true,
  "priority": 100,
  "conditions": [
    { "field": "video_codec", "op": "equal",
      "value": ["h264", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"],
      "connector": "and" },
    { "field": "audio_codec", "op": "equal",
      "value": ["flac"],
      "connector": "or" }
  ],
  "target_video_codec": "hevc",
  "target_audio_codec": "aac"
}
```

How it reads: the `or` connector on the second clause splits the conditions into two groups, so it matches when **either** is true:

- **Video group** — primary video codec is one of `h264`, `av1`, `vp9`, `mpeg4`, `mpeg2video`, or `vc1` (i.e. anything that's not already HEVC).
- **Audio group** — any audio track is FLAC.

Anything matching either group gets re-encoded to HEVC video + AAC audio; tracks that already match the target are stream-copied (so a file with HEVC video but a FLAC track only re-encodes the FLAC). Matching files that are *already* HEVC + AAC across the board produce no work — every stream gets `copy` and the rescan logs "nothing to do".

You can build this in the UI clause-by-clause; the JSON above is what's stored in the DB and what you'd see in a `/system/backup` export.

### Rescan with a specific workflow

The Rescan button on a series/movie page lets you pick a workflow from a dropdown. The chosen workflow is still evaluated against the file's conditions — picking "Convert AV1 to HEVC" on an HEVC file won't fire it. This way you can re-trigger a specific rule without rearranging priorities.

## Hardware acceleration

The image bundles ffmpeg with libx265 (CPU) plus VAAPI / NVENC / QSV support. The host needs to expose its GPU into the container:

| GPU | Compose |
|---|---|
| NVIDIA | `deploy.resources.reservations.devices` block with `capabilities: [gpu, compute, utility, video]`. Requires `nvidia-container-toolkit` on the host. |
| Intel / AMD VAAPI | `devices: ["/dev/dri:/dev/dri"]` + `group_add: ["video", "render"]` |
| CPU only | nothing — libx265 always works. |

Convertarr auto-detects the best available encoder at startup. Override per-node from **Settings → Nodes** if it picks the wrong one.

Note: AV1 NVDEC requires Ada Lovelace (RTX 4060+); on older NVIDIA cards Convertarr automatically SW-decodes AV1 sources. 10-bit H.264 (Hi10P) is also SW-decoded on VAAPI/QSV because most implementations don't support it.

## Multi-node setup

Run Convertarr on a second machine the same way (Docker or bare-metal). Then on the **host's** Settings → Nodes page, paste the worker's address + API key (find both under that worker's Settings → General). The host pairs over HTTP; the worker switches into worker-mode within ~5 seconds and starts pulling jobs.

Each worker decides its own `Max concurrent jobs` (Settings → General on that machine). Workers configure their own Sonarr/Radarr Path Mappings, so the host can dispatch *arr-relative paths and the worker resolves them locally — useful when host and worker have different mount layouts.

Disconnect with the **Disconnect from host** button on the worker, or **Unpair** from the host's Nodes page.

## Configuration reference

All runtime config is editable from the web UI under **Settings**:

- **General** — auth (forms / none), API key, max concurrent jobs (this node), delete-originals toggle, encoder override.
- **Sonarr / Radarr** — instances + path mappings.
- **Workflows** — codec-conversion rules.
- **Nodes** — pair / unpair workers, view their encoder + version.

A handful of immutable settings live in env vars (prefix `CONVERTARR_`), useful for headless setups: `CONVERTARR_DATA_DIR`, `CONVERTARR_DB_URL`, `CONVERTARR_HOST_URL_HINT` (overrides the host URL sent to workers when pairing).

## Status

Convertarr is in active development. The default flow is non-destructive — failed/cancelled encodes never touch the original — but always confirm with a test library first. Originals are moved to a sibling `.convertarr-backup/` folder when delete-originals is off.

Source: <https://github.com/OPvault/convertarr> · Issues: <https://github.com/OPvault/convertarr/issues>
