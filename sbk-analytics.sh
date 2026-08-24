#!/usr/bin/env bash
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Self-contained launcher for sbk-analytics on Linux and macOS. It acquires a
# verified uv binary and an isolated Python runtime when the host has neither.

set -u

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
readonly BOOTSTRAP_POLICY_FILE="$SCRIPT_DIR/sbk-bootstrap.env"

log() {
    printf '[sbk-analytics] %s\n' "$*" >&2
}

fail() {
    log "ERROR: $*"
    exit 1
}

[[ -r "$BOOTSTRAP_POLICY_FILE" ]] ||
    fail "bootstrap policy is missing: $BOOTSTRAP_POLICY_FILE"
# This trusted, shipped file contains simple launcher policy assignments.
# shellcheck disable=SC1090
. "$BOOTSTRAP_POLICY_FILE"

policy_error() {
    fail "invalid bootstrap policy $1: $2"
}

validate_version() {
    [[ "$2" =~ ^[0-9]+([.][0-9]+)+$ ]] ||
        policy_error "$1" "expected a dotted numeric version"
}

validate_leaf_name() {
    if [[ -z "$2" || "$2" == "." || "$2" == ".." ||
          "$2" == */* || "$2" == *\\* ]]; then
        policy_error "$1" "expected a non-empty filename without path separators"
    fi
}

validate_version "SBK_ANALYTICS_PYTHON_VERSION" \
    "${SBK_ANALYTICS_PYTHON_VERSION-}"
validate_version "SBK_ANALYTICS_UV_VERSION" "${SBK_ANALYTICS_UV_VERSION-}"
validate_leaf_name "SBK_ANALYTICS_RUNTIME_FOLDER" \
    "${SBK_ANALYTICS_RUNTIME_FOLDER-}"
validate_leaf_name "SBK_ANALYTICS_BOOTSTRAP_MARKER" \
    "${SBK_ANALYTICS_BOOTSTRAP_MARKER-}"

readonly PYTHON_VERSION="$SBK_ANALYTICS_PYTHON_VERSION"
readonly UV_VERSION="$SBK_ANALYTICS_UV_VERSION"
readonly BOOTSTRAP_MARKER="$SBK_ANALYTICS_BOOTSTRAP_MARKER"
readonly UV_RELEASE_BASE_DEFAULT="https://github.com/astral-sh/uv/releases/download"

case "$(uname -s 2>/dev/null || true)" in
    Linux)
        PLATFORM_OS=linux
        if [[ -n "${XDG_STATE_HOME:-}" ]]; then
            DEFAULT_STATE_ROOT="$XDG_STATE_HOME"
        elif [[ -n "${HOME:-}" ]]; then
            DEFAULT_STATE_ROOT="$HOME/.local/state"
        elif [[ -n "${SBK_ANALYTICS_ENV_HOME:-}" ]]; then
            DEFAULT_STATE_ROOT="$SBK_ANALYTICS_ENV_HOME"
        else
            fail "HOME, XDG_STATE_HOME, or SBK_ANALYTICS_ENV_HOME is required"
        fi
        ;;
    Darwin)
        PLATFORM_OS=macos
        if [[ -n "${HOME:-}" ]]; then
            DEFAULT_STATE_ROOT="$HOME/Library/Application Support"
        elif [[ -n "${SBK_ANALYTICS_ENV_HOME:-}" ]]; then
            DEFAULT_STATE_ROOT="$SBK_ANALYTICS_ENV_HOME"
        else
            fail "HOME or SBK_ANALYTICS_ENV_HOME is required"
        fi
        ;;
    *) fail "this launcher supports Linux and macOS only" ;;
esac
case "$(uname -m 2>/dev/null || true)" in
    x86_64|amd64) PLATFORM_ARCH=x86_64 ;;
    arm64|aarch64) PLATFORM_ARCH=aarch64 ;;
    *) fail "unsupported processor architecture: $(uname -m 2>/dev/null || true)" ;;
esac

case "$PLATFORM_OS-$PLATFORM_ARCH" in
    linux-x86_64)
        UV_TARGET=x86_64-unknown-linux-gnu
        UV_ARCHIVE_SHA256="${SBK_ANALYTICS_UV_LINUX_X86_64_SHA256-}"
        ;;
    linux-aarch64)
        UV_TARGET=aarch64-unknown-linux-gnu
        UV_ARCHIVE_SHA256="${SBK_ANALYTICS_UV_LINUX_AARCH64_SHA256-}"
        ;;
    macos-x86_64)
        UV_TARGET=x86_64-apple-darwin
        UV_ARCHIVE_SHA256="${SBK_ANALYTICS_UV_MACOS_X86_64_SHA256-}"
        ;;
    macos-aarch64)
        UV_TARGET=aarch64-apple-darwin
        UV_ARCHIVE_SHA256="${SBK_ANALYTICS_UV_MACOS_AARCH64_SHA256-}"
        ;;
esac
[[ "$UV_ARCHIVE_SHA256" =~ ^[0-9a-f]{64}$ ]] ||
    policy_error "uv checksum" "expected 64 lowercase hexadecimal characters"

readonly PLATFORM_ID="$PLATFORM_OS-$PLATFORM_ARCH"
readonly RUNTIME_HOME="${SBK_ANALYTICS_ENV_HOME:-$DEFAULT_STATE_ROOT/$SBK_ANALYTICS_RUNTIME_FOLDER}"
readonly MANAGED_UV_CACHE="$RUNTIME_HOME/cache/uv"
readonly MANAGED_PYTHON_ROOT="$RUNTIME_HOME/python"
readonly UV_TOOL_ROOT="$RUNTIME_HOME/tools/uv/$UV_VERSION/$UV_TARGET"
readonly UV_BINARY="$UV_TOOL_ROOT/uv"
readonly UV_BINARY_MARKER="$UV_TOOL_ROOT/uv.sha256"
readonly APP_ROOT="$RUNTIME_HOME/app"
readonly LOCK_ROOT="$RUNTIME_HOME/locks"

CURRENT_LOCK=
CURRENT_STAGE=
cleanup() {
    if [[ -n "$CURRENT_STAGE" && -d "$CURRENT_STAGE" ]]; then
        rm -rf "$CURRENT_STAGE"
    fi
    if [[ -n "$CURRENT_LOCK" && -d "$CURRENT_LOCK" ]]; then
        rm -rf "$CURRENT_LOCK"
    fi
}
handle_signal() {
    local signal_number="$1"
    cleanup
    trap - EXIT HUP INT TERM
    exit $((128 + signal_number))
}

trap cleanup EXIT
trap 'handle_signal 1' HUP
trap 'handle_signal 2' INT
trap 'handle_signal 15' TERM

sha256_stream() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 | awk '{print $NF}'
    else
        fail "sha256sum, shasum, or openssl is required to verify bootstrap artifacts"
    fi
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    elif command -v openssl >/dev/null 2>&1; then
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    else
        fail "sha256sum, shasum, or openssl is required to verify bootstrap artifacts"
    fi
}

source_fingerprint() {
    {
        printf 'schema=3\npython=%s\nuv=%s\nplatform=%s\n' \
            "$PYTHON_VERSION" "$UV_VERSION" "$PLATFORM_ID"
        for path in \
            "$SCRIPT_DIR/pyproject.toml" \
            "$SCRIPT_DIR/uv.lock" \
            "$SCRIPT_DIR/.python-version" \
            "$SCRIPT_DIR/sbk-bootstrap.env" \
            "$SCRIPT_DIR/sbk-config.env" \
            "$SCRIPT_DIR/requirements.txt" \
            "$SCRIPT_DIR/environment.yml" \
            "$SCRIPT_DIR/MANIFEST.in" \
            "$SCRIPT_DIR/sbk-analytics" \
            "$SCRIPT_DIR/sbk-analytics.sh"; do
            [[ -f "$path" ]] || fail "bootstrap input is missing: $path"
            printf '%s %s\n' "${path#"$SCRIPT_DIR/"}" "$(sha256_file "$path")"
        done
        while IFS= read -r path; do
            printf '%s %s\n' "${path#"$SCRIPT_DIR/"}" "$(sha256_file "$path")"
        done < <(find "$SCRIPT_DIR/analytics" "$SCRIPT_DIR/examples" -type f \
            \( -name '*.py' -o -name '*.txt' -o -name '*.env' \
               -o -name '*.yml' -o -name '*.yaml' \) | LC_ALL=C sort)
    } | sha256_stream
}

acquire_lock() {
    local lock="$1" attempt owner
    mkdir -p "$LOCK_ROOT" || fail "cannot create lock folder: $LOCK_ROOT"
    for ((attempt = 1; attempt <= 120; attempt++)); do
        if mkdir "$lock" 2>/dev/null; then
            printf '%s\n' "$$" >"$lock/pid"
            CURRENT_LOCK="$lock"
            return 0
        fi
        owner="$(sed -n '1p' "$lock/pid" 2>/dev/null || true)"
        if [[ "$owner" =~ ^[0-9]+$ ]] && ! kill -0 "$owner" 2>/dev/null; then
            rm -rf "$lock"
            continue
        fi
        [[ "$attempt" -eq 1 ]] && log "waiting for bootstrap lock: $lock"
        sleep 1
    done
    fail "timed out waiting for bootstrap lock: $lock"
}

release_lock() {
    [[ -n "$CURRENT_LOCK" ]] || return 0
    rm -rf "$CURRENT_LOCK"
    CURRENT_LOCK=
}

download_file() {
    local url="$1" destination="$2"
    case "$url" in
        https://*) ;;
        *)
            [[ "${SBK_ANALYTICS_BOOTSTRAP_ALLOW_INSECURE:-0}" == "1" ]] ||
                fail "bootstrap downloads require HTTPS: $url"
            ;;
    esac
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --silent --show-error \
            --output "$destination" "$url" >&2
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet --output-document="$destination" "$url" >&2
    else
        fail "curl or wget is required for the first bootstrap"
    fi
}

uv_is_ready() {
    local expected actual
    [[ -x "$UV_BINARY" && -r "$UV_BINARY_MARKER" ]] || return 1
    expected="$(sed -n '1p' "$UV_BINARY_MARKER")"
    actual="$(sha256_file "$UV_BINARY")"
    [[ "$expected" == "$actual" ]] || return 1
    "$UV_BINARY" --version >/dev/null 2>&1
}

ensure_uv() {
    local archive url extracted publish
    if [[ -n "${SBK_ANALYTICS_UV_EXECUTABLE:-}" ]]; then
        [[ -x "$SBK_ANALYTICS_UV_EXECUTABLE" ]] ||
            fail "SBK_ANALYTICS_UV_EXECUTABLE is not executable: $SBK_ANALYTICS_UV_EXECUTABLE"
        printf '%s\n' "$SBK_ANALYTICS_UV_EXECUTABLE"
        return 0
    fi
    if uv_is_ready; then
        printf '%s\n' "$UV_BINARY"
        return 0
    fi
    acquire_lock "$LOCK_ROOT/uv-$UV_VERSION-$UV_TARGET.lock"
    if uv_is_ready; then
        release_lock
        printf '%s\n' "$UV_BINARY"
        return 0
    fi
    CURRENT_STAGE="$UV_TOOL_ROOT.install-$$"
    rm -rf "$CURRENT_STAGE"
    mkdir -p "$CURRENT_STAGE" || fail "cannot stage uv under $CURRENT_STAGE"
    archive="$CURRENT_STAGE/uv.tar.gz"
    url="${SBK_ANALYTICS_UV_BASE_URL:-$UV_RELEASE_BASE_DEFAULT}/$UV_VERSION/uv-$UV_TARGET.tar.gz"
    log "downloading verified uv $UV_VERSION for $PLATFORM_ID"
    download_file "$url" "$archive" || fail "could not download uv from $url"
    [[ "$(sha256_file "$archive")" == "$UV_ARCHIVE_SHA256" ]] ||
        fail "uv archive checksum mismatch for $PLATFORM_ID"
    tar -xzf "$archive" -C "$CURRENT_STAGE" || fail "could not extract uv archive"
    extracted="$(find "$CURRENT_STAGE" -type f -name uv | head -n 1)"
    [[ -n "$extracted" && -x "$extracted" ]] ||
        fail "uv archive did not contain an executable"
    publish="$CURRENT_STAGE/publish"
    mkdir -p "$publish"
    cp "$extracted" "$publish/uv"
    chmod +x "$publish/uv"
    sha256_file "$publish/uv" >"$publish/uv.sha256"
    "$publish/uv" --version >/dev/null 2>&1 ||
        fail "downloaded uv failed its health check"
    mkdir -p "$(dirname -- "$UV_TOOL_ROOT")"
    rm -rf "$UV_TOOL_ROOT"
    mv "$publish" "$UV_TOOL_ROOT" || fail "could not publish uv"
    rm -rf "$CURRENT_STAGE"
    CURRENT_STAGE=
    release_lock
    uv_is_ready || fail "installed uv failed its health check"
    printf '%s\n' "$UV_BINARY"
}

app_python() {
    printf '%s/bin/python\n' "$1"
}

app_is_ready() {
    local env_root="$1" fingerprint="$2" python_bin
    python_bin="$(app_python "$env_root")"
    [[ -x "$python_bin" && -r "$env_root/$BOOTSTRAP_MARKER" ]] || return 1
    [[ "$(sed -n '1p' "$env_root/$BOOTSTRAP_MARKER")" == "$fingerprint" ]] || return 1
    PYTHONPATH= PYTHONHOME= "$python_bin" -P -c '
import pathlib, sys
import analytics, openpyxl, openpyxl_image_loader, PIL, psutil, requests, yaml
root = pathlib.Path(sys.prefix).resolve()
module = pathlib.Path(analytics.__file__).resolve()
raise SystemExit(root not in module.parents)
' >/dev/null 2>&1
}

bootstrap_application() {
    local fingerprint="$1" env_root="$2" uv_bin="$3" python_bin lock offline
    lock="$LOCK_ROOT/app-$fingerprint.lock"
    acquire_lock "$lock"
    if app_is_ready "$env_root" "$fingerprint"; then
        release_lock
        return 0
    fi
    CURRENT_STAGE="$APP_ROOT/.$fingerprint.install-$$"
    rm -rf "$CURRENT_STAGE"
    mkdir -p "$APP_ROOT" "$MANAGED_UV_CACHE" "$MANAGED_PYTHON_ROOT"
    # macOS still ships Bash 3.2, where expanding an empty array under `set -u`
    # raises an unbound-variable error. Keep this optional argument scalar.
    offline=
    [[ "${SBK_ANALYTICS_BOOTSTRAP_OFFLINE:-0}" == "1" ]] && offline=--offline
    log "preparing isolated Python $PYTHON_VERSION runtime"
    UV_CACHE_DIR="$MANAGED_UV_CACHE" UV_PYTHON_INSTALL_DIR="$MANAGED_PYTHON_ROOT" \
        "$uv_bin" python install --no-bin "$PYTHON_VERSION" \
        ${offline:+"$offline"} >&2 ||
        fail "could not install managed Python $PYTHON_VERSION"
    UV_CACHE_DIR="$MANAGED_UV_CACHE" UV_PYTHON_INSTALL_DIR="$MANAGED_PYTHON_ROOT" \
        "$uv_bin" venv --managed-python --python "$PYTHON_VERSION" \
        "$CURRENT_STAGE" ${offline:+"$offline"} >&2 || fail "could not create application environment"
    python_bin="$(app_python "$CURRENT_STAGE")"
    log "installing locked sbk-analytics environment"
    (
        cd "$SCRIPT_DIR" || exit 1
        VIRTUAL_ENV="$CURRENT_STAGE" UV_CACHE_DIR="$MANAGED_UV_CACHE" \
            UV_PYTHON_INSTALL_DIR="$MANAGED_PYTHON_ROOT" \
            "$uv_bin" sync --active --locked --no-editable --no-dev \
            --reinstall-package sbk-analytics --python "$python_bin" \
            ${offline:+"$offline"} >&2
    ) || fail "could not install the locked application environment"
    printf '%s\n' "$fingerprint" >"$CURRENT_STAGE/$BOOTSTRAP_MARKER"
    printf '{"schema":2,"fingerprint":"%s","python":"%s","platform":"%s","uv":"%s"}\n' \
        "$fingerprint" "$PYTHON_VERSION" "$PLATFORM_ID" "$UV_VERSION" \
        >"$CURRENT_STAGE/metadata.json"
    app_is_ready "$CURRENT_STAGE" "$fingerprint" ||
        fail "new application environment failed its health check"
    if [[ -d "$env_root" ]]; then
        rm -rf "$env_root"
    fi
    mv "$CURRENT_STAGE" "$env_root" || fail "could not publish application environment"
    CURRENT_STAGE=
    release_lock
}

unset VIRTUAL_ENV CONDA_PREFIX PYTHONPATH PYTHONHOME 2>/dev/null || true
FINGERPRINT="$(source_fingerprint)" || fail "could not fingerprint application sources"
APP_ENV="$APP_ROOT/$FINGERPRINT"

if ! app_is_ready "$APP_ENV" "$FINGERPRINT"; then
    UV="$(ensure_uv)" || fail "could not prepare the stage-zero runtime"
    bootstrap_application "$FINGERPRINT" "$APP_ENV" "$UV"
fi

PYTHON="$(app_python "$APP_ENV")"
export VIRTUAL_ENV="$APP_ENV"
export SBK_ANALYTICS_SOURCE_ROOT="$SCRIPT_DIR"
unset CONDA_PREFIX 2>/dev/null || true
unset PYTHONPATH PYTHONHOME 2>/dev/null || true
export PATH="$APP_ENV/bin:$PATH"
log "using managed application environment: $APP_ENV"
trap - EXIT HUP INT TERM
exec "$PYTHON" -P -m analytics "$@"
