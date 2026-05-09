# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build the venv ----------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src/ ./src/

RUN pip install --upgrade pip \
    && pip install .


# ---------- Stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# ffmpeg covers libx265 (CPU), NVENC (when /dev/nvidia* is passed in), and
# VAAPI (when /dev/dri is passed in). The VA driver packages provide the
# userspace bits VAAPI needs on Intel/AMD GPUs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        tini \
        gosu \
        tzdata \
        libva2 \
        libva-drm2 \
        mesa-va-drivers \
    && rm -rf /var/lib/apt/lists/*

# Default convertarr account at uid/gid 1000. The entrypoint reshuffles
# this to whatever PUID/PGID the user passes at runtime, so files written
# to the /config bind-mount land with sensible ownership on the host.
RUN groupadd --gid 1000 convertarr \
    && useradd --uid 1000 --gid convertarr --shell /bin/bash --create-home convertarr

COPY --from=builder /opt/venv /opt/venv
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONVERTARR_DATA_DIR=/config \
    CONVERTARR_DB_URL=sqlite:////config/convertarr.db \
    PUID=1000 \
    PGID=1000

# Persist the DB, logs, and any user uploads here. Bind-mount this on the host.
VOLUME ["/config"]

EXPOSE 6565

# Note: we deliberately don't set USER here. The entrypoint enters as root,
# fixes ownership on /config, then drops to the requested PUID/PGID via gosu.

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:6565/login', timeout=3).status < 500 else 1)" \
        || exit 1

# tini reaps ffmpeg children cleanly and forwards SIGTERM so docker-stop is
# a graceful shutdown, not a kill.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["convertarr"]
