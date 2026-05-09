#!/usr/bin/env bash
# run.sh — bootstrap + launch convertarr.
#
# What it does:
#   - creates .venv if missing
#   - installs the package (editable) on first run, or when pyproject.toml
#     has changed since the last install
#   - launches `convertarr` with --reload by default
#   - any args you pass go straight to convertarr (e.g. ./run.sh --no-reload)
#
# Usage:
#   ./run.sh                  # dev mode, auto-reload on src/ changes
#   ./run.sh --no-reload      # production-ish, no watcher
#   FORCE_INSTALL=1 ./run.sh  # force reinstall even if stamp is fresh
#
# Convertarr always binds 0.0.0.0:6565. Other runtime config (auth, codec
# policy, etc.) lives in the DB and is editable from Settings → General.

set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
STAMP="$VENV/.install-stamp"
RELOAD="--reload"
MIN_PY_MAJOR=3
MIN_PY_MINOR=12

# Strip --no-reload from forwarded args; everything else passes through.
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --no-reload) RELOAD="" ;;
        *) ARGS+=("$arg") ;;
    esac
done

# Returns 0 if "$1 -c ..." reports a version >= MIN_PY_MAJOR.MIN_PY_MINOR.
py_meets_min() {
    local bin="$1"
    command -v "$bin" >/dev/null 2>&1 || return 1
    "$bin" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR, $MIN_PY_MINOR) else 1)" 2>/dev/null
}

find_python() {
    # Honour explicit override first.
    if [[ -n "${PYTHON_BIN:-}" ]] && py_meets_min "$PYTHON_BIN"; then
        echo "$PYTHON_BIN"
        return 0
    fi
    # System Pythons, newest-first.
    for cand in python3.14 python3.13 python3.12 python3; do
        if py_meets_min "$cand"; then
            command -v "$cand"
            return 0
        fi
    done
    # uv-managed Python (installed by install_python_via_uv).
    local uv_bin
    if uv_bin="$(_uv_bin)" && [[ -n "$uv_bin" ]]; then
        local uv_py
        if uv_py="$("$uv_bin" python find ">=${MIN_PY_MAJOR}.${MIN_PY_MINOR}" 2>/dev/null)" \
            && [[ -n "$uv_py" ]] && py_meets_min "$uv_py"; then
            echo "$uv_py"
            return 0
        fi
    fi
    return 1
}

# Locate the `uv` binary, including locations the installer drops it into
# without touching PATH (~/.local/bin, ~/.cargo/bin).
_uv_bin() {
    if command -v uv >/dev/null 2>&1; then command -v uv; return 0; fi
    for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        [[ -x "$p" ]] && { echo "$p"; return 0; }
    done
    return 1
}

install_python_via_pkg_mgr() {
    if command -v pacman >/dev/null 2>&1; then
        # Arch / CachyOS / Manjaro: `python` is always the latest stable (>=3.12).
        sudo pacman -S --needed --noconfirm python
    elif command -v apt-get >/dev/null 2>&1; then
        # Ubuntu/Debian: only try the distro package. Skip deadsnakes — it
        # depends on Launchpad reachability, which is flaky in the wild;
        # uv fallback below is more reliable.
        sudo apt-get update
        sudo apt-get install -y python3.12 python3.12-venv
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3.12
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y python312
    else
        return 1
    fi
}

install_python_via_uv() {
    echo "[run.sh] installing uv-managed Python (portable, no sudo)"
    if ! _uv_bin >/dev/null; then
        echo "[run.sh] installing uv"
        if command -v curl >/dev/null 2>&1; then
            curl -LsSf https://astral.sh/uv/install.sh | sh
        elif command -v wget >/dev/null 2>&1; then
            wget -qO- https://astral.sh/uv/install.sh | sh
        else
            echo "[run.sh] ERROR: need curl or wget to bootstrap uv" >&2
            return 1
        fi
    fi
    local uv_bin
    uv_bin="$(_uv_bin)" || { echo "[run.sh] ERROR: uv installer did not produce a uv binary" >&2; return 1; }
    "$uv_bin" python install "${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
}

install_python() {
    echo "[run.sh] no Python >=${MIN_PY_MAJOR}.${MIN_PY_MINOR} found — attempting install"
    # Prefer system package manager (integrates with the OS); fall back to uv
    # if the distro doesn't have a new enough Python or the install fails.
    if install_python_via_pkg_mgr && find_python >/dev/null; then
        return 0
    fi
    install_python_via_uv
}

# 1. resolve a usable Python (install if necessary)
if ! PYTHON_BIN="$(find_python)"; then
    install_python
    if ! PYTHON_BIN="$(find_python)"; then
        echo "[run.sh] ERROR: install completed but no Python >=${MIN_PY_MAJOR}.${MIN_PY_MINOR} on PATH" >&2
        exit 1
    fi
fi
echo "[run.sh] using $PYTHON_BIN ($("$PYTHON_BIN" -c 'import sys; print(sys.version.split()[0])'))"

# 2. venv — recreate if it was built with an outdated Python.
if [[ -d "$VENV" ]] && ! py_meets_min "$VENV/bin/python"; then
    echo "[run.sh] existing venv uses stale Python — recreating"
    rm -rf "$VENV"
fi
if [[ ! -d "$VENV" ]]; then
    echo "[run.sh] creating venv at $VENV"
    "$PYTHON_BIN" -m venv "$VENV"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 3. install if needed (first run, pyproject changed, or FORCE_INSTALL=1)
needs_install=0
if [[ ! -f "$STAMP" ]]; then
    needs_install=1
elif [[ "pyproject.toml" -nt "$STAMP" ]]; then
    needs_install=1
elif [[ -n "${FORCE_INSTALL:-}" ]]; then
    needs_install=1
fi

if (( needs_install )); then
    echo "[run.sh] installing dependencies (editable)"
    pip install --quiet --upgrade pip
    pip install --quiet -e ".[dev]"
    touch "$STAMP"
else
    echo "[run.sh] deps up-to-date — skipping install (FORCE_INSTALL=1 to override)"
fi

# 4. launch
echo "[run.sh] launching convertarr ${RELOAD} ${ARGS[*]:-}"
exec convertarr ${RELOAD} "${ARGS[@]}"
