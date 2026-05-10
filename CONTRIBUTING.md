# Contributing to Convertarr

Thanks for considering a contribution. Convertarr is maintained by a single developer in their spare time, so a little structure goes a long way — please read this before opening an issue or PR.

## Ways to contribute

- **Report a bug** using the bug-report issue template.
- **Suggest a feature** using the feature-request issue template.
- **Ask a question** in [Discussions](https://github.com/opvault/convertarr/discussions) rather than opening an issue.
- **Send a pull request** for a bug fix or feature (see below — please open an issue first for anything non-trivial).
- **Report a security vulnerability** privately — see [SECURITY.md](SECURITY.md). Do not file a public issue.

## Before you open an issue

1. Search [open and closed issues](https://github.com/opvault/convertarr/issues?q=) for an existing report.
2. Reproduce on the latest released version (`:latest` Docker image or the most recent `v*` tag).
3. Have your version, deployment mode (bare metal vs. Docker, host vs. paired worker), and ffmpeg version ready — the issue template will ask for them.

## Development setup

Convertarr targets Python 3.12+ and ffmpeg 6.0+.

```sh
git clone https://github.com/opvault/convertarr
cd convertarr
./run.sh                # creates .venv, installs in editable mode, runs with --reload
```

The app binds `0.0.0.0:6565`. State lives in `data/` (bare metal) or `/config` (Docker). See [CLAUDE.md](CLAUDE.md) for the architecture map and known gotchas — it's the fastest way to get oriented.

To run against Docker instead:

```sh
docker compose up -d
```

## Pull requests

**Please open an issue first** for anything beyond a small bug fix or doc tweak. It's frustrating for both of us if you spend hours on a PR I then have to reject because it doesn't fit the project's direction.

Guidelines for the PR itself:

- **One concern per PR.** Bug fixes, refactors, and features go in separate PRs.
- **Keep the diff focused.** Don't reformat unrelated code; don't add abstractions you don't need yet.
- **Match existing style.** Read a few nearby files before writing new ones.
- **Update [CLAUDE.md](CLAUDE.md)** if you add a new architectural piece, change a convention, or hit a gotcha worth recording.
- **Test the change manually.** Convertarr's surface (hwaccel detection, *arr ingest, host/worker dispatch) is hard to cover with unit tests, so describe what you tried in the PR body — versions, codecs, GPU/CPU, deployment mode.
- **Don't bump the version.** Releases are cut by the maintainer.

### Commit messages

Conventional-commits style is preferred but not enforced:

```
fix: stop UI freeze when ffmpeg reports speed=N/A
feat(workflows): add codec-family condition operator
docs: clarify pairing token rotation in SECURITY.md
```

Keep the subject under ~70 characters. Use the body to explain *why*, not *what*.

### Branching

- Branch off `main`.
- PRs target `main`.
- The maintainer cuts release tags (`v1.x.y`) from `main`; the Docker workflow handles publishing.

## Reporting bugs effectively

The most useful bug reports include:

- Convertarr version (or image tag / commit SHA).
- Deployment: bare metal (`run.sh`) or Docker, host-only or paired host+worker.
- ffmpeg version (`ffmpeg -version | head -1`) and, if relevant, GPU model and driver.
- The workflow rule that triggered the job (or "rescan with no workflow match").
- The job log from `Settings → Logs` or `data/logs/job-N.log` — attached as a file, not pasted, if it's long.
- What you expected vs. what happened.

## Code of conduct

Convertarr doesn't ship a formal CoC. Be kind, assume good faith, and remember the maintainer is doing this in spare time. Abusive or harassing behaviour in issues, PRs, or discussions will get you blocked — that's the entire policy.

## Licence

By contributing, you agree that your contributions will be licensed under the same licence as the project (see [LICENSE](LICENSE)).
