#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Download and cache SBK + sbk-charts release artifacts from GitHub.

SBK is shipped as a `.tar` (or `.tar.gz`/`.zip`) containing a `bin/` directory with
`sbk-yal` and `sbk-gem-yal` shell scripts. sbk-charts is a Python package, installed
into an isolated venv via pip from the GitHub release tag.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import venv
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ..errors import CacheError, DependencyResolutionError, LocalPackageError
from ..policy import (
    APPLICATION,
    JDK_ARTIFACT,
    RUNTIME_POLICY,
    SBK_ARTIFACT,
    SBK_CHARTS_ARTIFACT,
)

log = logging.getLogger(__name__)
CACHE_POLICY = RUNTIME_POLICY.cache
DEPENDENCY_POLICY = RUNTIME_POLICY.dependencies
NETWORK_POLICY = RUNTIME_POLICY.network
LAYOUT_POLICY = RUNTIME_POLICY.dependency_layout
PROVENANCE_POLICY = RUNTIME_POLICY.provenance
ENVIRONMENT_POLICY = RUNTIME_POLICY.environment
DISPLAY_POLICY = RUNTIME_POLICY.display
CACHE_METADATA_POLICY = RUNTIME_POLICY.cache_metadata
DIAGNOSTIC_FIELDS = RUNTIME_POLICY.diagnostics
ARCHIVE_POLICY = RUNTIME_POLICY.archives


def _pip_trusted_host_args() -> list[str]:
    """Return pip CLI arguments for the centrally approved insecure hosts."""
    return [
        item
        for host in NETWORK_POLICY.pip_trusted_hosts
        for item in (NETWORK_POLICY.pip_trusted_host_option, host)
    ]


def _run_pip(cmd: list[str], pip_env: dict[str, str]) -> None:
    """Run pip with all installer output kept off machine-readable stdout."""
    try:
        sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        # Embedded callers and tests may replace stderr with an in-memory
        # stream that Popen cannot inherit. Capture and relay in that case.
        result = subprocess.run(
            cmd,
            check=True,
            env=pip_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.stdout:
            print(result.stdout, end="", file=sys.stderr, flush=True)
        return
    subprocess.run(
        cmd, check=True, env=pip_env, stdout=sys.stderr, stderr=sys.stderr
    )


def _entrypoint_interpreter_ready(command: Path) -> bool:
    """Return whether an absolute interpreter named by a script exists."""
    try:
        with command.open("rb") as stream:
            first_line = stream.readline()
    except OSError:
        return False
    if not first_line.startswith(b"#!"):
        return True
    interpreter = first_line[2:].strip().split(maxsplit=1)[0]
    if not interpreter or not os.path.isabs(os.fsdecode(interpreter)):
        return True
    return Path(os.fsdecode(interpreter)).is_file()


def _relocate_venv_scripts(venv_dir: Path, final_venv_dir: Path) -> None:
    """Rewrite absolute venv paths before an atomically staged publication."""
    old_prefix = os.fsencode(venv_dir)
    new_prefix = os.fsencode(final_venv_dir)
    bindir = venv_dir / LAYOUT_POLICY.executable_directory
    for command in bindir.iterdir():
        if not command.is_file():
            continue
        try:
            with command.open("rb") as stream:
                first_line = stream.readline()
        except OSError:
            continue
        if not first_line.startswith(b"#!" + old_prefix):
            continue
        content = command.read_bytes()
        command.write_bytes(content.replace(old_prefix, new_prefix, 1))

    configuration = venv_dir / LAYOUT_POLICY.virtual_environment_configuration
    if configuration.is_file():
        content = configuration.read_bytes()
        configuration.write_bytes(content.replace(old_prefix, new_prefix))


def cache_root() -> Path:
    """Return the environment-selected cache, then the platform default."""
    root = os.environ.get(ENVIRONMENT_POLICY.downloads_folder) or os.environ.get(
        ENVIRONMENT_POLICY.legacy_cache_folder
    )
    if root:
        return Path(root)
    return Path.home() / ".cache" / APPLICATION.name


def _cache_root() -> Path:
    """Backward-compatible private alias used by older callers/tests."""
    return cache_root()


def _cache_lock_path(cache: Path) -> Path:
    """Return the policy-defined lock path for a managed cache entry."""
    return cache.parent / CACHE_POLICY.lock_name_template.format(name=cache.name)


def _cache_stage_path(cache: Path) -> Path:
    """Return the policy-defined process-specific staging path."""
    return cache.with_name(
        CACHE_POLICY.install_stage_template.format(
            name=cache.name, pid=os.getpid()
        )
    )


class DependencySource(str, Enum):
    """How a resolved dependency was obtained for this invocation."""

    LOCAL = "LOCAL"
    MANAGED_CACHE = "MANAGED_CACHE"
    DOWNLOADED = "DOWNLOADED"


@dataclass(frozen=True)
class SourceProvenance:
    """Read-only origin details for a resolved dependency.

    ``dirty`` describes tracked-file changes. Untracked files are deliberately
    excluded so provenance does not add a full checkout scan to normal runs.
    """

    mode: str
    layout: str
    configured_location: str | None = None
    resolved_location: str | None = None
    repository_url: str | None = None
    release_tag: str | None = None
    asset: str | None = None
    sha256: str | None = None
    revision: str | None = None
    dirty: bool | None = None

    def as_dict(self) -> dict[str, str | bool | None]:
        return asdict(self)


@dataclass
class SbkInstall:
    home: Path  # selected SBK distribution root (contains bin/)
    source: DependencySource = DependencySource.MANAGED_CACHE
    _sbk_yal: Path | None = None
    _sbk_gem_yal: Path | None = None
    detected_version: str | None = None
    provenance: SourceProvenance | None = None

    @property
    def sbk_yal(self) -> Path:
        return (
            self._sbk_yal
            or self.home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.primary_executable
        )

    @property
    def sbk_gem_yal(self) -> Path | None:
        if self._sbk_gem_yal is not None:
            return self._sbk_gem_yal
        default = (
            self.home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.additional_executables[0]
        )
        return default if default.is_file() else None


@dataclass
class JdkInstall:
    home: Path  # extracted JDK home (contains bin/java)

    @property
    def java(self) -> Path:
        return (
            self.home / LAYOUT_POLICY.executable_directory
            / JDK_ARTIFACT.primary_executable
        )


@dataclass
class ChartsInstall:
    venv_dir: Path  # selected sbk-charts checkout or environment root
    source: DependencySource = DependencySource.MANAGED_CACHE
    _cli: Path | None = None
    _python: Path | None = None
    detected_version: str | None = None
    provenance: SourceProvenance | None = None

    @property
    def cli(self) -> Path:
        if self._cli is not None:
            return self._cli
        return (
            self.venv_dir / LAYOUT_POLICY.executable_directory
            / SBK_CHARTS_ARTIFACT.primary_executable
        )

    @property
    def python(self) -> Path:
        if self._python is not None:
            return self._python
        return (
            self.venv_dir / LAYOUT_POLICY.executable_directory
            / LAYOUT_POLICY.python_executable
        )


# ---------- helpers ----------


def _local_directory(folder: Path, dependency: str) -> Path:
    """Return a canonical local dependency directory or fail clearly.

    An explicitly configured local folder is authoritative: callers must not
    fall back to a download when this validation fails.
    """
    try:
        root = folder.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LocalPackageError(
            f"{dependency} local folder does not exist: {folder}"
        ) from exc
    if not root.is_dir():
        raise LocalPackageError(
            f"{dependency} local folder is not a directory: {root}"
        )
    return root


def _require_executable(path: Path, dependency: str) -> Path:
    """Validate a local command without modifying its permissions."""
    if not path.is_file():
        raise LocalPackageError(f"{dependency} executable is missing: {path}")
    if not os.access(path, os.X_OK):
        raise LocalPackageError(f"{dependency} executable is not executable: {path}")
    return path


def _git_details(path: Path) -> tuple[str | None, bool | None]:
    """Return a checkout's revision and tracked dirty state read-only."""
    checkout = path if path.is_dir() else path.parent
    if not (checkout / LAYOUT_POLICY.git_metadata).exists():
        return None, None

    def run(*args: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                [PROVENANCE_POLICY.git_command, "-C", str(checkout), *args],
                capture_output=True,
                text=True,
                timeout=DEPENDENCY_POLICY.source_control_timeout_s,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug(
                "Git provenance command failed for %s: %s: %s",
                checkout,
                " ".join(args),
                exc,
            )
            return None

    revision_result = run(*PROVENANCE_POLICY.git_revision_arguments)
    status_result = run(*PROVENANCE_POLICY.git_status_arguments)
    revision = (
        revision_result.stdout.strip()
        if revision_result is not None and revision_result.returncode == 0
        else None
    )
    dirty = (
        bool(status_result.stdout.strip())
        if status_result is not None and status_result.returncode == 0
        else None
    )
    if revision_result is not None and revision_result.returncode != 0:
        log.debug(
            "Git revision inspection failed for %s (rc=%s): %s",
            checkout,
            revision_result.returncode,
            (revision_result.stderr or "").strip(),
        )
    if status_result is not None and status_result.returncode != 0:
        log.debug(
            "Git status inspection failed for %s (rc=%s): %s",
            checkout,
            status_result.returncode,
            (status_result.stderr or "").strip(),
        )
    return revision or None, dirty


def _shared_provenance(
    configured: Path, resolved: Path, layout: str
) -> SourceProvenance:
    configured_path = configured.expanduser().resolve()
    revision, dirty = _git_details(configured_path)
    return SourceProvenance(
        mode=PROVENANCE_POLICY.shared_folder_mode,
        layout=layout,
        configured_location=str(configured_path),
        resolved_location=str(resolved),
        revision=revision,
        dirty=dirty,
    )


def _read_metadata(path: Path) -> dict:
    try:
        value = json.loads(
            path.read_text(encoding=DISPLAY_POLICY.text_encoding)
        )
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def managed_metadata(cache: Path) -> dict:
    """Read managed-install metadata for diagnostics without changing it."""
    return _read_metadata(cache / CACHE_POLICY.metadata_filename)


def _release_provenance(
    *, repository_url: str, version: str, resolved: Path,
    metadata: dict | None = None,
) -> SourceProvenance:
    values = metadata or {}
    return SourceProvenance(
        mode=PROVENANCE_POLICY.github_release_mode,
        layout=PROVENANCE_POLICY.managed_install_layout,
        resolved_location=str(resolved),
        repository_url=repository_url,
        release_tag=version,
        asset=values.get(CACHE_METADATA_POLICY.asset),
        sha256=(
            values.get(CACHE_METADATA_POLICY.sha256)
            or values.get(CACHE_METADATA_POLICY.source_sha256)
        ),
    )


def _command_version(command: Path, args: list[str], pattern: str) -> str | None:
    try:
        result = subprocess.run(
            [str(command), *args], capture_output=True, text=True,
            timeout=DEPENDENCY_POLICY.command_version_timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    match = re.search(pattern, stdout + stderr, re.I)
    return match.group(1) if match else None


def _check_version(name: str, detected: str | None, expected: str, policy: str) -> None:
    if policy == DEPENDENCY_POLICY.ignore_version_policy:
        return
    if detected == expected:
        return
    message = (
        f"{name} version mismatch: configured {expected!r}, "
        f"detected {detected or DISPLAY_POLICY.unknown_value!r}"
    )
    if policy == DEPENDENCY_POLICY.exact_version_policy:
        raise LocalPackageError(message)
    log.warning("%s (policy=warn)", message)


def _gh_release(
    repo: str,
    tag: str,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
) -> dict:
    """Fetch GitHub release metadata, accepting plain and ``v``-prefixed tags."""
    headers = {
        NETWORK_POLICY.github_accept_header: NETWORK_POLICY.github_accept_value,
        NETWORK_POLICY.github_api_version_header:
            NETWORK_POLICY.github_api_version,
    }
    token = os.environ.get(NETWORK_POLICY.github_token_environment)
    if token:
        headers[NETWORK_POLICY.authorization_header] = (
            f"{NETWORK_POLICY.bearer_prefix}{token}"
        )

    # Use ssl_verify setting from sbk-config.env
    if not ssl_verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log.warning("SSL verification DISABLED (ssl.verify=false in sbk-config.env)")
    else:
        log.debug("SSL verification enabled (ssl.verify=true in sbk-config.env)")

    prefix = NETWORK_POLICY.release_tag_prefix
    candidates = (
        (tag,)
        if tag.lower().startswith(prefix)
        else (tag, f"{prefix}{tag}")
    )
    for candidate in candidates:
        url = NETWORK_POLICY.github_api_url.format(repo=repo, tag=candidate)
        log.info("fetching GitHub release metadata: %s@%s", repo, candidate)
        response = requests.get(
            url,
            headers=headers,
            timeout=NETWORK_POLICY.github_metadata_timeout_s,
            verify=ssl_verify,
        )
        if response.status_code == HTTPStatus.NOT_FOUND:
            continue
        response.raise_for_status()
        metadata = response.json()
        if candidate != tag:
            log.info("resolved configured release %s via GitHub tag %s", tag, candidate)
        return metadata
    attempted = ", ".join(f"{repo}@{candidate}" for candidate in candidates)
    raise RuntimeError(f"GitHub release not found; attempted: {attempted}")


def _download(
    url: str, dest: Path, *,
    max_attempts: int = NETWORK_POLICY.artifact_download_attempts,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
) -> str:
    """Download `url` to `dest`, resuming via HTTP Range if .part already exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + CACHE_POLICY.partial_download_suffix)
    last_err: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {
            NETWORK_POLICY.range_header:
                NETWORK_POLICY.byte_range_template.format(offset=offset)
        } if offset else {}
        log.info(
            "downloading %s -> %s (attempt %d/%d, offset=%d, SSL verify=%s)",
            url, dest, attempt, max_attempts, offset, ssl_verify,
        )
        try:
            with requests.get(
                url,
                stream=True,
                timeout=NETWORK_POLICY.artifact_download_timeout_s,
                headers=headers,
                verify=ssl_verify,
            ) as r:
                if offset and r.status_code == HTTPStatus.OK:
                    # server ignored Range; restart from scratch
                    tmp.unlink(missing_ok=True)
                    offset = 0
                elif r.status_code not in (
                    HTTPStatus.OK,
                    HTTPStatus.PARTIAL_CONTENT,
                ):
                    r.raise_for_status()

                # Get total file size for progress reporting
                total_size = int(
                    r.headers.get(NETWORK_POLICY.content_length_header, 0)
                )
                if offset:
                    total_size += offset

                mode = "ab" if offset else "wb"
                with tmp.open(mode) as f:
                    downloaded = offset
                    last_progress_time = time.time()
                    last_progress_size = downloaded

                    for chunk in r.iter_content(
                        chunk_size=NETWORK_POLICY.download_chunk_bytes
                    ):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                            # Show progress every 2 seconds
                            current_time = time.time()
                            if (
                                current_time - last_progress_time
                                >= NETWORK_POLICY.download_progress_interval_s
                            ):
                                if total_size > 0:
                                    percent = (
                                        downloaded / total_size
                                    ) * DISPLAY_POLICY.percentage_scale
                                    bytes_per_mebibyte = (
                                        DISPLAY_POLICY.bytes_per_kibibyte ** 2
                                    )
                                    mb_downloaded = downloaded / bytes_per_mebibyte
                                    mb_total = total_size / bytes_per_mebibyte
                                    speed = (
                                        (downloaded - last_progress_size)
                                        / (current_time - last_progress_time)
                                        / bytes_per_mebibyte
                                    )
                                    progress_msg = f"  Download progress: {percent:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB, {speed:.1f} MB/s)"
                                    log.info(progress_msg)
                                    print(progress_msg, flush=True)
                                else:
                                    mb_downloaded = downloaded / (
                                        DISPLAY_POLICY.bytes_per_kibibyte ** 2
                                    )
                                    progress_msg = f"  Downloaded: {mb_downloaded:.1f} MB"
                                    log.info(progress_msg)
                                    print(progress_msg, flush=True)

                                last_progress_time = current_time
                                last_progress_size = downloaded

                # Final progress report
                if total_size > 0:
                    percent = (
                        downloaded / total_size
                    ) * DISPLAY_POLICY.percentage_scale
                    bytes_per_mebibyte = DISPLAY_POLICY.bytes_per_kibibyte ** 2
                    mb_downloaded = downloaded / bytes_per_mebibyte
                    mb_total = total_size / bytes_per_mebibyte
                    progress_msg = f"  Download complete: {percent:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB)"
                    log.info(progress_msg)
                    print(progress_msg, flush=True)
                else:
                    mb_downloaded = downloaded / (
                        DISPLAY_POLICY.bytes_per_kibibyte ** 2
                    )
                    progress_msg = f"  Download complete: {mb_downloaded:.1f} MB"
                    log.info(progress_msg)
                    print(progress_msg, flush=True)

            tmp.replace(dest)
            digest = hashlib.sha256()
            with dest.open("rb") as downloaded_file:
                for block in iter(
                    lambda: downloaded_file.read(
                        NETWORK_POLICY.download_chunk_bytes
                    ),
                    b"",
                ):
                    digest.update(block)
            return digest.hexdigest()
        except (requests.exceptions.SSLError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_err = e
            wait = min(2 ** attempt, NETWORK_POLICY.download_retry_cap_s)
            log.warning(
                "download interrupted (%s); retrying in %ds (got %d bytes so far)",
                e.__class__.__name__, wait, tmp.stat().st_size if tmp.exists() else 0,
            )
            time.sleep(wait)
    raise DependencyResolutionError(
        f"failed to download {url} after {max_attempts} attempts"
    ) from last_err


@contextmanager
def _cache_lock(lock_path: Path):
    """Serialize installers that target the same dependency/version."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_metadata(path: Path, values: dict[str, Any]) -> None:
    values[CACHE_METADATA_POLICY.installed_at] = (
        datetime.now(timezone.utc).isoformat()
    )
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")


def _extract(archive: Path, dest: Path) -> Path:
    """Extract archive into dest/, return the (single) top-level dir inside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    destination = dest.resolve()

    def safe(member_name: str) -> None:
        candidate = (destination / member_name).resolve()
        if candidate != destination and destination not in candidate.parents:
            raise CacheError(f"unsafe archive path rejected: {member_name}")

    if name.endswith(ARCHIVE_POLICY.zip_suffix):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                safe(member.filename)
                mode = member.external_attr >> ARCHIVE_POLICY.member_mode_shift
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise CacheError(f"archive symlink rejected: {member.filename}")
            zf.extractall(dest)
    elif name.endswith(ARCHIVE_POLICY.tar_suffixes):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                safe(member.name)
                if member.issym() or member.islnk():
                    raise CacheError(f"archive link rejected: {member.name}")
                if member.isdev() or member.isfifo():
                    raise CacheError(
                        f"archive special file rejected: {member.name}"
                    )
            tf.extractall(dest)
    else:
        raise RuntimeError(f"unsupported archive type: {archive.name}")

    # locate top-level dir
    entries = [p for p in dest.iterdir() if p.is_dir()]
    if len(entries) == 1:
        return entries[0]
    return dest
