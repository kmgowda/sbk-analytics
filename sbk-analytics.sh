#!/usr/bin/env bash
# Self-bootstrapping launcher for sbk-analytics on Linux and macOS.

set -u

readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=9
readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
readonly ENV_HOME="${SBK_ANALYTICS_ENV_HOME:-$SCRIPT_DIR}"
readonly MANAGED_VENV="$ENV_HOME/.venv"
readonly MANAGED_CONDA="$ENV_HOME/.conda"

log() {
    printf '[sbk-analytics] %s\n' "$*" >&2
}

fail() {
    log "ERROR: $*"
    exit 1
}

is_supported_python() {
    local python_bin="$1"
    [[ -x "$python_bin" ]] || return 1
    "$python_bin" -c \
        "import sys; raise SystemExit(sys.version_info < ($MIN_PYTHON_MAJOR, $MIN_PYTHON_MINOR))" \
        >/dev/null 2>&1
}

find_system_python() {
    local candidate resolved
    for candidate in \
        "${SBK_ANALYTICS_PYTHON:-}" \
        python3 python \
        python3.13 python3.12 python3.11 python3.10 python3.9; do
        [[ -n "$candidate" ]] || continue
        resolved="$(command -v "$candidate" 2>/dev/null || true)"
        if [[ -n "$resolved" ]] && is_supported_python "$resolved"; then
            printf '%s\n' "$resolved"
            return 0
        fi
    done
    return 1
}

environment_fingerprint() {
    local python_bin="$1"
    "$python_bin" -c '
import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
digest = hashlib.sha256(str(root).encode())
for name in ("pyproject.toml", "requirements.txt", "environment.yml", "sbk-analytics.sh"):
    path = root / name
    if path.is_file():
        digest.update(name.encode())
        digest.update(path.read_bytes())
print(digest.hexdigest())
' "$SCRIPT_DIR"
}

environment_is_ready() {
    local python_bin="$1" env_root="$2" fingerprint marker
    marker="$env_root/.sbk-analytics-bootstrap"
    fingerprint="$(environment_fingerprint "$python_bin")" || return 1
    [[ -r "$marker" ]] || return 1
    [[ "$(<"$marker")" == "$fingerprint" ]] || return 1
    "$python_bin" -c '
import os
import pathlib
import sys

import analytics
import openpyxl
import openpyxl_image_loader
import PIL
import psutil
import requests
import yaml

source = pathlib.Path(sys.argv[1]).resolve()
module = pathlib.Path(analytics.__file__).resolve()
raise SystemExit(os.path.commonpath((str(source), str(module))) != str(source))
' "$SCRIPT_DIR" >/dev/null 2>&1
}

bootstrap_environment() {
    local python_bin="$1" env_root="$2" fingerprint
    if environment_is_ready "$python_bin" "$env_root"; then
        return 0
    fi
    log "installing sbk-analytics and its Python dependencies into $env_root"
    "$python_bin" -m ensurepip --upgrade >/dev/null 2>&1 || true
    "$python_bin" -m pip install --disable-pip-version-check -e "$SCRIPT_DIR" >&2 || return 1
    fingerprint="$(environment_fingerprint "$python_bin")" || return 1
    printf '%s\n' "$fingerprint" >"$env_root/.sbk-analytics-bootstrap" || return 1
}

activate_and_run() {
    local kind="$1" env_root="$2" python_bin="$3"
    shift 3
    if [[ "$kind" == "conda" ]]; then
        export CONDA_PREFIX="$env_root"
        unset VIRTUAL_ENV 2>/dev/null || true
    else
        export VIRTUAL_ENV="$env_root"
        unset CONDA_PREFIX 2>/dev/null || true
    fi
    export PATH="$env_root/bin:$PATH"
    log "using $kind environment: $env_root"
    exec "$python_bin" -m analytics "$@"
}

try_existing_environment() {
    local kind="$1" env_root="$2" python_bin
    python_bin="$env_root/bin/python"
    [[ -n "$env_root" ]] || return 1
    is_supported_python "$python_bin" || return 1
    if bootstrap_environment "$python_bin" "$env_root"; then
        activate_and_run "$kind" "$env_root" "$python_bin" "${CLI_ARGS[@]}"
    fi
    log "could not prepare the existing $kind environment at $env_root"
    return 1
}

create_or_repair_venv() {
    local system_python="$1" python_bin
    python_bin="$MANAGED_VENV/bin/python"
    mkdir -p "$ENV_HOME" || return 1
    log "creating Python virtual environment: $MANAGED_VENV"
    "$system_python" -m venv "$MANAGED_VENV" || return 1
    is_supported_python "$python_bin" || return 1
    bootstrap_environment "$python_bin" "$MANAGED_VENV" || return 1
    activate_and_run venv "$MANAGED_VENV" "$python_bin" "${CLI_ARGS[@]}"
}

create_or_repair_conda() {
    local conda_bin python_bin
    python_bin="$MANAGED_CONDA/bin/python"
    conda_bin="$(command -v conda 2>/dev/null || true)"
    [[ -n "$conda_bin" ]] || return 1
    mkdir -p "$ENV_HOME" || return 1
    if ! is_supported_python "$python_bin"; then
        log "creating fallback Conda environment: $MANAGED_CONDA"
        "$conda_bin" create --yes --prefix "$MANAGED_CONDA" python=3.10 pip >&2 || return 1
    fi
    is_supported_python "$python_bin" || return 1
    bootstrap_environment "$python_bin" "$MANAGED_CONDA" || return 1
    activate_and_run conda "$MANAGED_CONDA" "$python_bin" "${CLI_ARGS[@]}"
}

case "$(uname -s 2>/dev/null || true)" in
    Linux|Darwin) ;;
    *) fail "this launcher supports Linux and macOS only" ;;
esac

CLI_ARGS=("$@")

# Reuse the caller's environment first. Activating another environment behind
# an explicitly active one would be surprising and can hide useful packages.
if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    try_existing_environment venv "$VIRTUAL_ENV" || true
fi
if [[ -n "${CONDA_PREFIX:-}" ]]; then
    try_existing_environment conda "$CONDA_PREFIX" || true
fi

# Reuse launcher-owned environments before creating anything new.
try_existing_environment venv "$MANAGED_VENV" || true
try_existing_environment conda "$MANAGED_CONDA" || true

SYSTEM_PYTHON="$(find_system_python || true)"
if [[ -n "$SYSTEM_PYTHON" ]]; then
    create_or_repair_venv "$SYSTEM_PYTHON" || \
        log "venv setup failed; trying Conda fallback"
fi

create_or_repair_conda || true

if [[ -z "$SYSTEM_PYTHON" ]] && ! command -v conda >/dev/null 2>&1; then
    fail "Python 3.9 or newer is required, and Conda is not available to provide it"
fi
fail "could not create a working venv or Conda environment; check the installation errors above"
