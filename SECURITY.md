# Security Policy

Thanks for helping keep Convertarr and its users safe. This document explains which versions receive security fixes and how to report a vulnerability.

## Supported Versions

Convertarr is distributed primarily as a Docker image at `ghcr.io/opvault/convertarr`. Security fixes are applied to the latest released minor version only. Older versions are not patched — please upgrade before reporting.

| Version       | Supported          |
| ------------- | ------------------ |
| 1.0.x         | :white_check_mark: |
| < 1.0         | :x:                |
| `:main` (dev) | Best effort        |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

Use GitHub's private vulnerability reporting instead:

1. Go to the [Security tab](https://github.com/opvault/convertarr/security) of this repository.
2. Click **Report a vulnerability**.
3. Fill in the advisory form with as much detail as you can.

If you cannot use GitHub's private reporting for any reason, send a direct message to the maintainer on X: [@olayzen](https://x.com/olayzen). Keep the initial message short ("convertarr security report — can I share details here?") and wait for a reply before sending sensitive details.

### What to include

A good report makes triage much faster. Please include:

- The Convertarr version (or image tag / commit SHA) you tested against.
- Deployment context (bare metal via `run.sh`, Docker, host vs. paired worker).
- A clear description of the vulnerability and its impact.
- Step-by-step reproduction instructions, ideally with a minimal proof of concept.
- Any relevant logs, request/response captures, or configuration snippets (with secrets redacted).
- Your assessment of severity and any suggested remediation.

### What to expect

- **Acknowledgement:** within 72 hours of receiving the report.
- **Initial triage:** within 7 days, including a severity assessment and whether the report is accepted.
- **Fix timeline:** depends on severity. Critical issues are prioritised over feature work; lower-severity issues are scheduled into the next release.
- **Disclosure:** coordinated. We will credit you in the release notes and GitHub Security Advisory unless you prefer to remain anonymous.

Please give us a reasonable window to ship a fix before any public disclosure.

## Scope

In scope:

- The Convertarr application code in this repository.
- The official Docker images published under `ghcr.io/opvault/convertarr`.
- The HTTP API surface (web UI, `/api/v1/nodes/*`, `/api/v1/pairing/*`).
- Host ↔ worker pairing and job dispatch.
- Handling of Sonarr/Radarr API keys and other credentials stored by Convertarr.

Out of scope:

- Vulnerabilities in third-party dependencies that have no exploitable path through Convertarr — please report those upstream.
- Issues that require a pre-compromised host, root access, or write access to Convertarr's data directory.
- Bugs in Sonarr, Radarr, Jellyfin, Plex, ffmpeg, or other external services Convertarr integrates with.
- Findings from automated scanners without a demonstrated, exploitable impact.
- Denial of service via obviously expensive operations (e.g. scheduling thousands of concurrent encodes against your own instance).

## Operator Security Notes

Convertarr is intended to run on a trusted LAN alongside your *arr stack. A few things worth knowing when deploying it:

- **Do not expose Convertarr directly to the public internet.** Put it behind a VPN, Tailscale, or an authenticating reverse proxy.
- **Protect the data directory** (`data/` for bare metal, `/config` in Docker). It contains the SQLite database with Sonarr/Radarr API keys and pairing tokens.
- **Pairing tokens grant job-dispatch access** between host and worker — treat them like passwords and rotate them if a node is decommissioned.
- **Path mappings are per-instance.** A misconfigured worker can read or overwrite any path it has filesystem access to; scope its mounts accordingly.

## Credit

We appreciate responsible disclosure. Reporters who follow this policy will be acknowledged in the relevant security advisory and release notes.
