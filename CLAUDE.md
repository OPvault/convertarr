# CLAUDE.md

Notes for Claude / contributors. Captures the things that aren't obvious from reading the code: why pieces are arranged the way they are, what bit us, and how to ship a release.

## What Convertarr is

A pre-encoder for the \*arr stack. Pulls file paths from Sonarr/Radarr, ffprobes them, and re-encodes anything Jellyfin/Plex would otherwise have to transcode at playback. **Workflows are the only conversion trigger** — without a workflow, rescan is a no-op. There's no global allowlist fallback anymore (used to exist, removed because it surprised users).

The app and the worker are the same binary; a single Convertarr instance runs in *host mode* (claims jobs from its own queue) or *worker mode* (paired to another instance, pulls jobs over HTTP). The supervisor at `src/convertarr/workers/supervisor.py` switches modes based on the `paired_host_url` runtime setting — no process restart needed.

## How to run

```sh
./run.sh                  # bare metal, dev mode (auto-reload)
./run.sh --no-reload      # production-ish

docker compose up -d      # using docker-compose.yml at repo root
```

`run.sh` handles venv + install. Bare-metal state lives under `data/`. Docker state lives under `/config` (set via `CONVERTARR_DATA_DIR=/config` and `CONVERTARR_DB_URL=sqlite:////config/convertarr.db` baked into the image).

## Architecture map

| Concern | Where |
|---|---|
| HTTP routes (web UI + queue/history/settings) | `web/routes.py` (large) |
| `/system/*` pages (logs, backup, statistics, about) | `web/system_routes.py` |
| `/api/v1/nodes/*` (worker → host: register, heartbeat, claim, finish) | `web/node_routes.py` |
| `/api/v1/pairing/*` (host → worker: accept, disconnect) | `web/pairing_routes.py` |
| Job dispatch: claim, execute, finalize, record | `workers/execution.py` |
| Host's built-in worker loop | `workers/local_node.py` |
| Remote worker loop (HTTP-mediated) | `worker/loop.py` |
| Mode supervisor (host vs worker) | `workers/supervisor.py` |
| Sonarr/Radarr ingest, ffprobe, queue | `workers/ingest.py` |
| Per-stream `copy`/`reencode`/`skip` decision | `probe/policy.py` (`evaluate()`) |
| Workflow matcher (which rule applies) | `workflows.py` |
| ffmpeg argv builder + hwaccel routing | `encode/plan.py` |
| ffmpeg subprocess + progress parser | `encode/runner.py` |
| Encoder auto-detect (NVENC/VAAPI/QSV/AMF/CPU) | `encode/hwdetect.py` |
| Codec display labels (`canonical_codec`, `format_conversion`) | `probe/codec_labels.py` |
| DB migrations (idempotent, PRAGMA-driven) | `db.py` |

## Conventions

**Migrations.** Add an `_migrate_*` function in `db.py` that reads `PRAGMA table_info(<table>)`, ALTERs only if columns are missing. Idempotent. Wire it from `init_db()`. SQLAlchemy's `create_all` doesn't ALTER existing tables — that's why we hand-roll.

**Codec display strings.** Use `probe/codec_labels.py`. `canonical_codec()` for one codec, `format_conversion(src, dst)` for "AV1 → HEVC (H.265)" chips (returns `None` when src == dst). Don't reach into `_CODEC_CANONICAL` directly — `system_routes.py` already imports it via the helper.

**Per-stream codec snapshots.** The `Job` row carries `source_video_codec` / `source_audio_codec` / `target_video_codec` / `target_audio_codec` so the dashboard's running-job poll (every 2 s) doesn't need to re-evaluate workflows or ffprobe. Stamped at:
- claim time on the host, in `execution.claim_for_node()`
- mirror-create time on the worker, in `worker/loop.py:_create_mirror_job`

**Comments.** Default to no comments. Add one only when the *why* is non-obvious — a hidden constraint, a workaround for a specific bug, behaviour that would surprise a reader. Never multi-line docstrings explaining what well-named identifiers already say.

**SQLite.** WAL + `synchronous=NORMAL` + `busy_timeout=5000` is set in `db.py`. Parallel writers queue briefly; readers never block. Long-running operations (ingest fan-out) work fine under this.

## Gotchas

These are landmines the test suite won't catch:

- **AV1 NVDEC needs Ada Lovelace (RTX 4060+).** Older NVIDIA GPUs claim support via `ffmpeg -hwaccels` but fail at runtime ("Your platform doesn't support hardware accelerated AV1 decoding"), producing zero frames and a silent encode failure. We dropped `av1` from the cuda hw-decode set in `encode/plan.py:_HW_DECODABLE_CODECS`.
- **10-bit H.264 (Hi10P) doesn't work on most VAAPI/QSV.** Same failure mode — "Failed setup for format vaapi". `_can_hw_decode()` checks `pix_fmt` and falls back to SW decode for `h264 + yuv420p10le` on these families. HEVC 10-bit (Main10) is fine.
- **`speed=N/A` from ffmpeg used to freeze the UI** because the parser kept the previous tick's value. Now `_parse_progress_line` resets to `None` so the wall-clock fallback recomputes each tick. Same fix for `fps`.
- **Worker `log_path` must come from the worker's data dir,** not the host's. The host's `claim_for_node` puts its own `/config/logs/job-N.log` into the dispatch; if the worker honours that path it `PermissionError`s before ffmpeg starts. The worker overrides `dispatch.log_path` to its own `settings.absolute_data_dir / "logs"` in `worker/loop.py`'s dispatch loop.
- **`max_concurrent_jobs` is worker-owned.** The worker reads its local `Node` row each tick and reports the value to the host on register/heartbeat. The host stores it for display only. Old code used to push it the other direction — don't go back.
- **Pairing `host_url` must come from the request's Host header,** not `socket.connect("8.8.8.8", 80)`. The socket trick returns the Docker bridge IP (e.g. `172.28.0.2`) inside a container, which a LAN worker can't reach. `routes.py:pair_node` uses `request.url` first, falls back to the socket trick only when there's no Host header.
- **Path translation lives on each instance, not per-node.** Remote workers receive the `*arr-relative path` in the dispatch and apply their own `ArrInstance.path_mappings` to derive the local view. There's no per-node mapping table — workers are configured exactly like a standalone install would be.
- **The ingest fan-out semaphore is process-wide** (`workers/ingest.py:_INGEST_SEMAPHORE`, capped at 8). Bulk-rescanning multiple series doesn't get to spam ffprobe; the cap holds across all rescans.
- **Workflow matching uses standard precedence.** Conditions are joined by `and` (default) or `or`; AND binds tighter, so `A and B or C and D` becomes `(A and B) or (C and D)`. First matching workflow wins (lowest priority number first).

## Release / publish flow

Images are at `ghcr.io/opvault/convertarr`. The CI workflow at `.github/workflows/docker.yml` does:

- **Push to `main`** → builds `:main`, `:latest`, `:sha-XXX`.
- **Push `v*` tag** → builds `:{version}`, `:sha-XXX`. Sliding tags (`:latest`, `:{major}`, `:{major}.{minor}`) only fire when this tag is the highest semver in the repo — so force-pushing v1.0.0 backward to a rollback commit can't clobber `:latest`. Also adds `:main` if the tag points at current main HEAD.
- **PR** → builds but doesn't push.

For manual cleanup / retag (e.g. consolidating tags after a force-push, sweeping orphaned dev builds), use:

```sh
python3 scripts/ghcr-tags.py --dry-run    # preview
python3 scripts/ghcr-tags.py              # apply TAG_MAP + prune orphans
```

`scripts/ghcr-tags.py` has a declarative `TAG_MAP` at the top — list of `(tags, source)` tuples. Sources you reference are protected from pruning. Auth uses `gh auth token` (needs `read:packages`, `write:packages`, `delete:packages` scopes).

`scripts/` is in `.gitignore` — these are operator tools, not committed to the repo.

## Commit messages

Semantic Commits — `<type>(<scope>): <subject>` (scope optional).

Types:
- `feat` — new user-visible feature
- `fix` — user-visible bug fix
- `docs` — documentation only
- `style` — formatting, whitespace, no code behaviour change
- `refactor` — production code change with no behaviour change
- `test` — adding or refactoring tests, no production change
- `chore` — build, deps, tooling, no production change

Subject rules:
- Present tense, lowercase, no trailing period.
- Concise but descriptive — readable without the diff.
- Use a scope when the change is clearly bound to a module/feature/file group.

Examples:
- `feat(auth): add OAuth2 login support`
- `fix(api): handle null response from payment gateway`
- `refactor(cart): simplify discount calculation logic`
- `chore: update dependencies`

**Always inspect the diff first** — `git diff --staged` (or `git diff HEAD` if nothing is staged). Base the message on what's actually changed, not on conversation context.

When the user asks for a commit message in isolation (no commit action), return only the message in a single fenced code block — no surrounding prose.

## Things NOT to do

- **Don't commit unless asked.** State the change is ready and let the user run `git commit` (or say "commit it for me").
- **Don't add error handling for impossible cases.** Trust framework guarantees and internal callers. Validate at system boundaries only.
- **Don't add backwards-compat shims** for code that's about to land in a single PR. Just change the call sites.
- **Don't delete `data/convertarr.db` during smoke tests.** It holds the user's actual Sonarr/Radarr config.
