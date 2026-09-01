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
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from .errors import CacheError, DependencyResolutionError, LocalPackageError
from .policy import (
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


def cache_root() -> Path:
    """Return the environment-selected cache, then the platform default."""
    root = os.environ.get("SBK_ANALYTICS_DOWNLOADS_FOLDER") or os.environ.get(
        "SBK_ANALYTICS_CACHE"
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
        return {
            "mode": self.mode,
            "layout": self.layout,
            "configured_location": self.configured_location,
            "resolved_location": self.resolved_location,
            "repository_url": self.repository_url,
            "release_tag": self.release_tag,
            "asset": self.asset,
            "sha256": self.sha256,
            "revision": self.revision,
            "dirty": self.dirty,
        }


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
        return _jdk_executable(self.home)


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


def _jdk_executable(home: Path) -> Path:
    return (
        home / LAYOUT_POLICY.executable_directory
        / JDK_ARTIFACT.primary_executable
    )


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


def _sbk_local_candidates(
    root: Path,
) -> tuple[tuple[Path, str, Path, Path], ...]:
    """Return the canonical SBK layouts in their resolution order."""
    candidates = (
        (root, PROVENANCE_POLICY.distribution_layout),
        (
            root.joinpath(*LAYOUT_POLICY.sbk_gradle_install_path),
            PROVENANCE_POLICY.gradle_install_layout,
        ),
    )
    return tuple(
        (
            home,
            layout,
            home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.primary_executable,
            home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.additional_executables[0],
        )
        for home, layout in candidates
    )


def _charts_local_candidates(
    root: Path, *, explicit_cli: Path | None = None,
) -> tuple[tuple[Path, str], ...]:
    """Return canonical sbk-charts commands in their resolution order."""
    if explicit_cli is not None:
        return ((explicit_cli, PROVENANCE_POLICY.explicit_executable_layout),)
    executable = SBK_CHARTS_ARTIFACT.primary_executable
    return (
        (root / executable, PROVENANCE_POLICY.source_launcher_layout),
        (
            root / LAYOUT_POLICY.executable_directory / executable,
            PROVENANCE_POLICY.environment_layout,
        ),
    )


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
        value = json.loads(path.read_text(encoding="utf-8"))
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
        asset=values.get("asset"),
        sha256=values.get("sha256") or values.get("source_sha256"),
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


def _charts_version(cli: Path, *, require_ready: bool = False) -> str | None:
    try:
        result = subprocess.run(
            [str(cli), "-h"], capture_output=True, text=True,
            timeout=DEPENDENCY_POLICY.charts_readiness_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        if require_ready:
            raise LocalPackageError(
                "sbk-charts readiness check timed out after "
                f"{DEPENDENCY_POLICY.charts_readiness_timeout_s:g}s: {cli}"
            ) from exc
        result = None
    if result is not None:
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        output = stdout + stderr
        match = re.search(
            r"Sbk Charts Version\s*:\s*(\d+(?:\.\d+)+)",
            output,
            re.I,
        )
        if require_ready and result.returncode != 0:
            raise LocalPackageError(
                f"sbk-charts readiness check failed (rc={result.returncode}): "
                f"{cli}; "
                f"{output.strip()[-DISPLAY_POLICY.diagnostic_tail_characters:]}"
            )
        if match:
            return match.group(1)
    python = cli.parent / LAYOUT_POLICY.python_executable
    if not python.is_file():
        return None
    return _command_version(
        python,
        [
            "-c",
            "import importlib.metadata as m; "
            f"print(m.version('{SBK_CHARTS_ARTIFACT.distribution_name}'))",
        ],
        r"(\d+(?:\.\d+)+)",
    )


def _check_version(name: str, detected: str | None, expected: str, policy: str) -> None:
    if policy == DEPENDENCY_POLICY.ignore_version_policy:
        return
    if detected == expected:
        return
    message = (
        f"{name} version mismatch: configured {expected!r}, "
        f"detected {detected or 'unknown'!r}"
    )
    if policy == DEPENDENCY_POLICY.exact_version_policy:
        raise LocalPackageError(message)
    log.warning("%s (policy=warn)", message)


def resolve_local_sbk(
    folder: Path, *, require_gem: bool = False, expected_version: str = "",
    version_policy: str = DEPENDENCY_POLICY.default_version_policy,
) -> SbkInstall:
    """Resolve a ready-to-run SBK distribution or built source checkout.

    Supported roots contain either ``bin/sbk-yal`` (a distribution) or
    ``build/install/sbk/bin/sbk-yal`` (a Gradle ``installDist`` checkout).
    The bounded list deliberately avoids selecting stale artifacts via a
    recursive filesystem search.
    """
    root = _local_directory(folder, "SBK")
    candidates = _sbk_local_candidates(root)
    for home, layout, sbk_yal, sbk_gem_yal in candidates:
        if not sbk_yal.is_file():
            continue
        resolved_gem = None
        if sbk_gem_yal.is_file() and os.access(sbk_gem_yal, os.X_OK):
            resolved_gem = sbk_gem_yal
        elif require_gem:
            _require_executable(
                sbk_gem_yal,
                f"SBK {SBK_ARTIFACT.additional_executables[0]}",
            )
        detected = _command_version(
            sbk_yal, ["-help"], r"SBK(?:-YAL)?\s+Version:\s*([^\s]+)"
        )
        if expected_version:
            _check_version("SBK", detected, expected_version, version_policy)
        return SbkInstall(
            home=home,
            source=DependencySource.LOCAL,
            _sbk_yal=_require_executable(
                sbk_yal, f"SBK {SBK_ARTIFACT.primary_executable}"
            ),
            _sbk_gem_yal=resolved_gem,
            detected_version=detected,
            provenance=_shared_provenance(
                folder,
                home,
                layout,
            ),
        )
    checked = ", ".join(
        str(sbk_yal) for _home, _layout, sbk_yal, _sbk_gem_yal in candidates
    )
    raise LocalPackageError(
        "SBK local folder is not a ready-to-run distribution or built "
        f"checkout: {root}; checked: {checked}"
    )


def resolve_local_sbk_charts(
    folder: Path | None = None, *, executable: Path | None = None,
    expected_version: str = "",
    version_policy: str = DEPENDENCY_POLICY.default_version_policy,
    preflight: bool = False,
) -> ChartsInstall:
    """Resolve a ready-to-run local sbk-charts checkout or environment."""
    if executable is not None:
        cli = executable.expanduser().resolve(strict=True)
        _require_executable(cli, SBK_CHARTS_ARTIFACT.display_name)
        root = cli.parent
        candidates = _charts_local_candidates(root, explicit_cli=cli)
        configured = executable
    elif folder is not None:
        root = _local_directory(folder, SBK_CHARTS_ARTIFACT.display_name)
        candidates = _charts_local_candidates(root)
        configured = folder
    else:
        raise LocalPackageError("sbk-charts local folder or executable is required")
    for cli, layout in candidates:
        if cli.is_file():
            resolved_cli = _require_executable(
                cli, SBK_CHARTS_ARTIFACT.display_name
            )
            detected = _charts_version(cli, require_ready=preflight)
            if expected_version:
                _check_version(
                    SBK_CHARTS_ARTIFACT.display_name,
                    detected,
                    expected_version,
                    version_policy,
                )
            return ChartsInstall(
                venv_dir=root,
                source=DependencySource.LOCAL,
                _cli=resolved_cli,
                _python=Path(sys.executable),
                detected_version=detected,
                provenance=_shared_provenance(
                    configured,
                    cli,
                    layout,
                ),
            )
    checked = ", ".join(str(candidate) for candidate, _layout in candidates)
    raise LocalPackageError(
        f"sbk-charts local folder has no supported executable: {root}; "
        f"checked: {checked}"
    )


def inspect_shared_sbk(folder: Path, *, require_gem: bool = False) -> dict:
    """Describe a shared SBK selection without executing or modifying it."""
    result: dict = {
        "configured_location": str(folder),
        "read_only": True,
        "build_performed": False,
        "valid": False,
    }
    try:
        root = _local_directory(folder, "SBK")
    except LocalPackageError as exc:
        result["error"] = str(exc)
        return result
    for home, layout, sbk_yal, sbk_gem_yal in _sbk_local_candidates(root):
        if not sbk_yal.is_file():
            continue
        yal_ready = sbk_yal.is_file() and os.access(sbk_yal, os.X_OK)
        gem_ready = sbk_gem_yal.is_file() and os.access(sbk_gem_yal, os.X_OK)
        provenance = _shared_provenance(root, home, layout)
        result.update({
            "valid": yal_ready and (gem_ready or not require_gem),
            "layout": layout,
            "resolved_location": str(home),
            "sbk_yal": str(sbk_yal),
            "sbk_yal_executable": yal_ready,
            "sbk_gem_yal": str(sbk_gem_yal),
            "sbk_gem_yal_executable": gem_ready,
            "revision": provenance.revision,
            "dirty": provenance.dirty,
        })
        if require_gem and not gem_ready:
            result["error"] = "GEM workload requires executable sbk-gem-yal"
        elif not yal_ready:
            result["error"] = (
                f"SBK {SBK_ARTIFACT.primary_executable} executable is not "
                f"executable: {sbk_yal}"
            )
        return result
    result["error"] = (
        "no executable sbk-yal in the distribution root or "
        "build/install/sbk; sbk-analytics does not build shared SBK folders"
    )
    return result


def inspect_shared_sbk_charts(
    folder: Path | None = None, *, executable: Path | None = None,
) -> dict:
    """Describe shared sbk-charts paths without starting or modifying them."""
    configured = executable or folder
    result: dict = {
        "configured_location": str(configured) if configured is not None else None,
        "read_only": True,
        "install_performed": False,
        "valid": False,
    }
    try:
        if executable is not None:
            cli = executable.expanduser().resolve(strict=True)
            root = cli.parent
            candidates = _charts_local_candidates(root, explicit_cli=cli)
            provenance_root = cli
        elif folder is not None:
            root = _local_directory(folder, SBK_CHARTS_ARTIFACT.display_name)
            candidates = _charts_local_candidates(root)
            provenance_root = root
        else:
            raise LocalPackageError(
                "sbk-charts local folder or executable is required"
            )
    except (LocalPackageError, OSError, RuntimeError) as exc:
        result["error"] = str(exc)
        return result
    for cli, layout in candidates:
        if not cli.is_file():
            continue
        ready = os.access(cli, os.X_OK)
        revision, dirty = _git_details(provenance_root)
        result.update({
            "valid": ready,
            "layout": layout,
            "resolved_location": str(root),
            "executable": str(cli),
            "executable_ready": ready,
            "revision": revision,
            "dirty": dirty,
        })
        if not ready:
            result["error"] = (
                f"{SBK_CHARTS_ARTIFACT.display_name} executable is not "
                f"executable: {cli}"
            )
        return result
    result["error"] = "no supported executable sbk-charts command found"
    return result


def _gh_release(
    repo: str,
    tag: str,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
) -> dict:
    """Fetch GitHub release metadata, accepting plain and ``v``-prefixed tags."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": NETWORK_POLICY.github_api_version,
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    # Use ssl_verify setting from sbk-config.env
    if not ssl_verify:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        log.warning("SSL verification DISABLED (ssl.verify=false in sbk-config.env)")
    else:
        log.debug("SSL verification enabled (ssl.verify=true in sbk-config.env)")
    
    candidates = (tag,) if tag.lower().startswith("v") else (tag, f"v{tag}")
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
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    
    for attempt in range(1, max_attempts + 1):
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
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
                total_size = int(r.headers.get('content-length', 0))
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


def _write_metadata(path: Path, **values) -> None:
    values["installed_at"] = datetime.now(timezone.utc).isoformat()
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

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                safe(member.filename)
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) == stat.S_IFLNK:
                    raise CacheError(f"archive symlink rejected: {member.filename}")
            zf.extractall(dest)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
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


# ---------- SBK ----------


def ensure_sbk(
    version: str,
    repo: str = SBK_ARTIFACT.repository_slug or "",
    downloads_folder: Path | None = None,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
    local_folder: Path | None = None,
    require_gem: bool = False,
    version_policy: str = DEPENDENCY_POLICY.default_version_policy,
) -> SbkInstall:
    """Resolve local SBK first, otherwise use/download the pinned release."""
    if local_folder is not None:
        log.info("using explicitly configured local SBK folder: %s", local_folder)
        return resolve_local_sbk(
            local_folder, require_gem=require_gem, expected_version=version,
            version_policy=version_policy,
        )

    # Use specified folder if provided, otherwise use cache
    if downloads_folder is None:
        cache = _cache_root() / SBK_ARTIFACT.cache_namespace / version
    else:
        cache = downloads_folder / version
        cache.mkdir(parents=True, exist_ok=True)
    
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(_cache_lock_path(cache)):
        return _ensure_sbk_locked(
            version, repo, cache, ssl_verify, require_gem
        )


def _ensure_sbk_locked(
    version: str, repo: str, cache: Path, ssl_verify: bool | str,
    require_gem: bool,
) -> SbkInstall:
    marker = cache / CACHE_POLICY.completion_marker
    home_file = cache / CACHE_POLICY.home_pointer

    if marker.exists() and home_file.exists():
        home = Path(home_file.read_text().strip())
        # Validate that the extracted distribution still has the binaries.
        has_yal = (
            home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.primary_executable
        ).exists()
        has_required_gem = (
            not require_gem
            or (
                home / LAYOUT_POLICY.executable_directory
                / SBK_ARTIFACT.additional_executables[0]
            ).exists()
        )
        if has_yal and has_required_gem:
            log.info("SBK %s already installed at %s (cache hit)", version, home)
            metadata = _read_metadata(cache / CACHE_POLICY.metadata_filename)
            return SbkInstall(
                home=home, source=DependencySource.MANAGED_CACHE,
                detected_version=version,
                provenance=_release_provenance(
                    repository_url=(
                        repo if "://" in repo
                        else f"{NETWORK_POLICY.github_web_url}/{repo}"
                    ),
                    version=version,
                    resolved=home,
                    metadata=metadata,
                ),
            )
        log.warning(
            "SBK %s cache marker exists but binaries missing at %s; re-installing",
            version, home,
        )
        marker.unlink(missing_ok=True)

    stage = _cache_stage_path(cache)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    log.info("fetching SBK release metadata: %s@%s", repo, version)
    rel = _gh_release(repo, version, ssl_verify=ssl_verify)
    assets = rel.get("assets") or []
    # Prefer a top-level distribution asset named like 'sbk-<ver>.tar' (not sbk-gem-yal-*)
    candidates = []
    for a in assets:
        n = a["name"].lower()
        if not n.startswith("sbk"):
            continue
        if not n.endswith((".tar", ".tar.gz", ".tgz", ".zip")):
            continue
        # de-prioritise sub-distros like sbk-gem-yal-X.tar
        score = 0 if n.startswith(("sbk-" + version.lower(), f"sbk-{version}")) else 1
        if "gem" in n or "yal" in n or "sbm" in n:
            score += 10
        candidates.append((score, a))

    if not candidates:
        raise RuntimeError(
            f"no SBK distribution archive found in release {version}; "
            f"assets: {[a['name'] for a in assets]}"
        )
    candidates.sort(key=lambda x: x[0])
    asset = candidates[0][1]
    url = asset["browser_download_url"]
    log.info("selected SBK asset: %s", asset["name"])

    archive = stage / Path(urlparse(url).path).name
    if not archive.exists():
        checksum = _download(url, archive, ssl_verify=ssl_verify)
    else:
        checksum = None
    # GitHub.com exposes `digest` for release assets. Older GitHub Enterprise
    # versions may omit it, in which case metadata still records our checksum.
    expected_digest = asset.get("digest")
    if expected_digest and expected_digest.startswith("sha256:"):
        expected_sha256 = expected_digest.split(":", 1)[1].lower()
        if checksum != expected_sha256:
            raise CacheError(
                f"SBK asset checksum mismatch for {asset['name']}: "
                f"expected {expected_sha256}, got {checksum}"
            )

    log.info("extracting SBK archive: %s", archive)
    extract_dir = stage / LAYOUT_POLICY.extracted_directory
    top = _extract(archive, extract_dir)

    # Find the directory that actually contains bin/sbk-yal
    home = top
    if not (
        home / LAYOUT_POLICY.executable_directory
        / SBK_ARTIFACT.primary_executable
    ).exists():
        for sub in home.rglob(LAYOUT_POLICY.executable_directory):
            if (sub / SBK_ARTIFACT.primary_executable).exists():
                home = sub.parent
                break

    # make scripts executable
    bindir = home / LAYOUT_POLICY.executable_directory
    if bindir.is_dir():
        for f in bindir.iterdir():
            try:
                f.chmod(f.stat().st_mode | 0o111)
            except OSError:
                pass

    _require_executable(
        home / LAYOUT_POLICY.executable_directory
        / SBK_ARTIFACT.primary_executable,
        f"downloaded SBK {SBK_ARTIFACT.primary_executable}",
    )
    if require_gem:
        _require_executable(
            home / LAYOUT_POLICY.executable_directory
            / SBK_ARTIFACT.additional_executables[0],
            f"downloaded SBK {SBK_ARTIFACT.additional_executables[0]}",
        )

    relative_home = home.relative_to(stage)
    final_home = cache / relative_home
    (stage / CACHE_POLICY.home_pointer).write_text(str(final_home.resolve()))
    _write_metadata(
        stage / CACHE_POLICY.metadata_filename,
        dependency=SBK_ARTIFACT.key, version=version,
        source_url=url, asset=asset["name"], sha256=checksum,
        executables={
            executable: str(
                final_home / LAYOUT_POLICY.executable_directory / executable
            )
            for executable in SBK_ARTIFACT.executables
        },
    )
    # Free disk: the ~1+ GB archive is no longer needed once extracted.
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    # Completion is written inside the staging directory and the entire
    # installation is then atomically published under the version path.
    (stage / CACHE_POLICY.completion_marker).touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    log.info("SBK %s ready at %s", version, final_home)
    return SbkInstall(
        home=final_home,
        source=DependencySource.DOWNLOADED,
        detected_version=version,
        provenance=_release_provenance(
            repository_url=(
                repo if "://" in repo
                else f"{NETWORK_POLICY.github_web_url}/{repo}"
            ),
            version=version,
            resolved=final_home,
            metadata={"asset": asset["name"], "sha256": checksum},
        ),
    )


# ---------- sbk-charts ----------


def ensure_sbk_charts(
    version: str,
    repo_url: str = SBK_CHARTS_ARTIFACT.repository_url,
    source_sha256: str | None = None,
    downloads_folder: Path | None = None,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
    local_folder: Path | None = None,
    local_executable: Path | None = None,
    version_policy: str = DEPENDENCY_POLICY.default_version_policy,
    preflight: bool = False,
) -> ChartsInstall:
    """Resolve local sbk-charts first, otherwise use its isolated cache."""
    # Local selection must precede conda detection so an explicit path is
    # always authoritative and never silently replaced by another package.
    if local_folder is not None or local_executable is not None:
        log.info(
            "using explicitly configured local sbk-charts folder: %s",
            local_executable or local_folder,
        )
        return resolve_local_sbk_charts(
            local_folder, executable=local_executable,
            expected_version=version, version_policy=version_policy,
            preflight=preflight,
        )

    # Use downloads_folder for caching if provided, otherwise use cache
    if downloads_folder is None:
        cache = _cache_root() / SBK_CHARTS_ARTIFACT.cache_namespace / version
    else:
        cache = downloads_folder / SBK_CHARTS_ARTIFACT.cache_namespace / version
        cache.mkdir(parents=True, exist_ok=True)
    
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(_cache_lock_path(cache)):
        install = _ensure_sbk_charts_locked(
            version, repo_url, cache, ssl_verify, source_sha256
        )
        if preflight:
            _charts_version(install.cli, require_ready=True)
        return install


def _ensure_sbk_charts_locked(
    version: str,
    repo_url: str,
    cache: Path,
    ssl_verify: bool | str,
    source_sha256: str | None,
) -> ChartsInstall:
    venv_dir = cache / LAYOUT_POLICY.virtual_environment_directory
    marker = cache / CACHE_POLICY.completion_marker

    install = ChartsInstall(
        venv_dir=venv_dir, source=DependencySource.MANAGED_CACHE
    )
    if marker.exists() and install.cli.exists() and install.python.exists():
        metadata_path = cache / CACHE_POLICY.metadata_filename
        metadata = _read_metadata(metadata_path)
        cached_digest = metadata.get("source_sha256")
        if source_sha256 is None or cached_digest == source_sha256:
            log.info(
                "sbk-charts %s already installed at %s (cache hit)",
                version,
                venv_dir,
            )
            install.provenance = _release_provenance(
                repository_url=repo_url,
                version=version,
                resolved=install.cli,
                metadata=metadata,
            )
            return install
        log.warning(
            "sbk-charts %s cache digest differs from configuration; "
            "re-installing",
            version,
        )
    if marker.exists():
        log.warning(
            "sbk-charts %s cache marker exists but venv is incomplete; re-installing",
            version,
        )
        marker.unlink(missing_ok=True)

    stage = _cache_stage_path(cache)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    stage_venv = stage / LAYOUT_POLICY.virtual_environment_directory
    install = ChartsInstall(
        venv_dir=stage_venv, source=DependencySource.DOWNLOADED
    )
    log.info("creating venv for sbk-charts %s at %s", version, stage_venv)
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(stage_venv)

    # Prefer the immutable, checksum-verified GitHub source archive. Custom
    # configurations without a digest retain the legacy git-tag fallback.
    source_archive = stage / f"{SBK_CHARTS_ARTIFACT.key}-{version}.tar.gz"
    source_url = (
        f"{repo_url.rstrip('/')}/archive/refs/tags/{quote(version, safe='')}.tar.gz"
    )
    if source_sha256 is not None:
        checksum = _download(source_url, source_archive, ssl_verify=ssl_verify)
        if checksum.lower() != source_sha256.lower():
            raise DependencyResolutionError(
                "sbk-charts source checksum mismatch: "
                f"expected {source_sha256}, got {checksum}"
            )
        spec = str(source_archive)
    else:
        pip_url = repo_url.rstrip("/")
        if not pip_url.endswith(LAYOUT_POLICY.git_url_suffix):
            pip_url = pip_url + LAYOUT_POLICY.git_url_suffix
        spec = f"git+{pip_url}@{version}"
        source_url = spec
        log.warning(
            "sbk-charts.sha256 is not configured; using the legacy git install"
        )

    # Build pip command with optional SSL verification control
    pip_env = os.environ.copy()
    pip_args = [
        str(install.python),
        "-m",
        NETWORK_POLICY.pip_module,
        NETWORK_POLICY.pip_install_subcommand,
    ]
    
    if not ssl_verify:
        pip_args.extend(_pip_trusted_host_args())
        # Also set environment variables for git
        pip_env[ENVIRONMENT_POLICY.git_ssl_no_verify] = (
            ENVIRONMENT_POLICY.enabled_value
        )
        log.warning("SSL verification DISABLED for pip (ssl.verify=false in sbk-config.env)")
    elif isinstance(ssl_verify, str):
        pip_env[ENVIRONMENT_POLICY.pip_cert] = ssl_verify
        pip_env[ENVIRONMENT_POLICY.git_ssl_ca_info] = ssl_verify
        log.info("using custom CA bundle for pip/git: %s", ssl_verify)
    else:
        log.debug("SSL verification enabled for pip (ssl.verify=true in sbk-config.env)")
    
    # Upgrade pip first
    cmd = pip_args + [
        NETWORK_POLICY.pip_quiet_option,
        NETWORK_POLICY.pip_upgrade_option,
        NETWORK_POLICY.pip_module,
    ]
    log.info("upgrading pip in venv")
    _run_pip(cmd, pip_env)
    
    # Install sbk-charts
    cmd = pip_args + [spec]
    log.info("installing sbk-charts: %s", spec)
    _run_pip(cmd, pip_env)
    if source_sha256 is not None:
        source_archive.unlink(missing_ok=True)

    if not install.cli.exists():
        # some versions expose differently named entry points
        bindir = stage_venv / LAYOUT_POLICY.executable_directory
        candidates = list(bindir.glob("sbk-charts*")) + list(bindir.glob("sb-charts*"))
        if candidates:
            install = ChartsInstall(
                venv_dir=stage_venv,
                source=DependencySource.DOWNLOADED,
                _cli=candidates[0],
            )
            log.warning(
                "sbk-charts CLI not at expected path; found: %s",
                [c.name for c in candidates],
            )
        else:
            raise RuntimeError(
                f"sbk-charts installed but no CLI script found under {bindir}"
            )

    # Publish only an environment whose real command starts successfully.
    # This catches missing transitive imports and broken console entry points,
    # not merely the presence of a generated script.
    _charts_version(install.cli, require_ready=True)

    relative_cli = install.cli.relative_to(stage)
    relative_python = install.python.relative_to(stage)
    final_cli = cache / relative_cli
    final_python = cache / relative_python
    _write_metadata(
        stage / CACHE_POLICY.metadata_filename,
        dependency=SBK_CHARTS_ARTIFACT.key, version=version,
        source_url=source_url, executable=str(final_cli),
        source_sha256=source_sha256, spec=spec,
    )
    (stage / CACHE_POLICY.completion_marker).touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    log.info("sbk-charts %s installed successfully", version)
    return ChartsInstall(
        venv_dir=cache / LAYOUT_POLICY.virtual_environment_directory,
        source=DependencySource.DOWNLOADED,
        _cli=final_cli,
        _python=final_python,
        provenance=_release_provenance(
            repository_url=repo_url,
            version=version,
            resolved=final_cli,
            metadata={"source_sha256": source_sha256},
        ),
    )


# ---------- JDK (Adoptium / Temurin) ----------

_JDK_VERSION_RE = re.compile(r'(?:openjdk|java)\s+version\s+"([^"]+)"')


def _java_major_version(java_path: Path) -> int | None:
    """Return the major version number reported by ``<java_path> -version``,
    or ``None`` if it cannot be determined.

    Handles both modern Java (``25.0.3``, ``21.0.5``, ``17.0.10``) and the
    legacy ``1.8.0_xxx`` form (where the major version is the second part).
    """
    try:
        proc = subprocess.run(
            [str(java_path), "-version"],
            capture_output=True, text=True,
            timeout=DEPENDENCY_POLICY.java_version_timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("java -version failed for %s: %s", java_path, e)
        return None
    output = (proc.stderr or "") + (proc.stdout or "")
    m = _JDK_VERSION_RE.search(output)
    if not m:
        return None
    parts = m.group(1).split(".")
    try:
        major = int(parts[0])
        if major == 1 and len(parts) >= 2:  # legacy 1.x layout
            major = int(parts[1])
        return major
    except (ValueError, IndexError):
        return None


def _candidate_jdk_homes() -> list[Path]:
    """Return JDK homes to probe, in priority order, deduplicated.
    
    Resolution order as specified:
    1. SBK_JAVA_HOME environment variable
    2. JAVA_HOME environment variable  
    3. Default installed java version (java on PATH)
    """
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _push(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        candidates.append(resolved)

    # 1. SBK_JAVA_HOME - highest priority
    v = os.environ.get(ENVIRONMENT_POLICY.sbk_java_home)
    if v:
        _push(Path(v))

    # 2. JAVA_HOME - second priority
    v = os.environ.get(ENVIRONMENT_POLICY.java_home)
    if v:
        _push(Path(v))

    # 3. java on PATH -> derive home as the parent of <home>/bin/java.
    from shutil import which
    java = which(JDK_ARTIFACT.primary_executable)
    if java:
        jp = Path(java)
        if jp.parent.name == LAYOUT_POLICY.executable_directory:
            _push(jp.parent.parent)

    return candidates


def find_existing_jdk(required_major: int) -> Path | None:
    """Return the home of an already-installed JDK whose major version
    matches ``required_major``, or ``None`` if no match is found.

    Probes (in order): ``SBK_JAVA_HOME`` env var, ``JAVA_HOME`` env var,
    then ``java`` on ``PATH``.
    """
    for home in _candidate_jdk_homes():
        java = _jdk_executable(home)
        if not java.is_file() or not os.access(java, os.X_OK):
            log.debug("skipping JDK candidate %s: no bin/java", home)
            continue
        major = _java_major_version(java)
        log.debug("candidate JDK %s reports major=%s", home, major)
        if major == required_major:
            log.info(
                "found pre-installed JDK %s at %s; skipping download",
                major, home,
            )
            return home
    log.info(
        "no pre-installed JDK %s found in SBK_JAVA_HOME / JAVA_HOME / PATH",
        required_major,
    )
    return None


def _jdk_platform() -> tuple[str, str]:
    arch = "x64" if os.uname().machine in ("x86_64", "amd64") else os.uname().machine
    try:
        os_name = {"linux": "linux", "darwin": "mac"}[sys.platform]
    except KeyError as exc:
        raise DependencyResolutionError(
            f"managed JDK installation is unsupported on {sys.platform}; "
            "sbk-analytics supports Linux and macOS"
        ) from exc
    return os_name, arch


def _jdk_asset(
    version: str, ssl_verify: bool | str
) -> tuple[str, str]:
    """Resolve the upstream Temurin package URL and published SHA-256."""
    os_name, arch = _jdk_platform()
    template = JDK_ARTIFACT.metadata_url_template
    if template is None:
        raise RuntimeError("JDK artifact metadata URL template is not configured")
    url = template.format(version=version, os=os_name, arch=arch)
    try:
        response = requests.get(
            url,
            timeout=NETWORK_POLICY.github_metadata_timeout_s,
            verify=ssl_verify,
        )
        response.raise_for_status()
        assets = response.json()
        package = assets[0]["binary"]["package"]
        download_url = str(package["link"])
        checksum = str(package["checksum"]).lower()
    except (requests.RequestException, ValueError, IndexError, KeyError, TypeError) as exc:
        raise DependencyResolutionError(
            f"could not resolve checksum-verified Temurin JDK {version} metadata"
        ) from exc
    if not re.fullmatch(r"[0-9a-f]{64}", checksum):
        raise DependencyResolutionError(
            f"Temurin JDK {version} metadata contains an invalid SHA-256"
        )
    if not download_url.startswith("https://"):
        raise DependencyResolutionError(
            f"Temurin JDK {version} metadata contains a non-HTTPS package URL"
        )
    return download_url, checksum


def ensure_jdk(
    version: str = DEPENDENCY_POLICY.default_jdk_version,
    jdk_folder: Path | None = None,
    ssl_verify: bool | str = DEPENDENCY_POLICY.default_ssl_verify,
) -> JdkInstall:
    """Ensure a JDK of the given major version is available.

    Resolution order (exactly as specified):

    1. **SBK_JAVA_HOME** -- if set and points to the required version, use it.
    2. **JAVA_HOME** -- if set and points to the required version, use it.
    3. **java on PATH** -- if it reports the required version, use it.
    4. **Specified folder** -- if jdk_folder is provided and contains the required version, use it.
    5. **Download** -- fetch Temurin of the requested major version from
       the Adoptium API, extract it under the specified folder (or cache if not specified),
       and return its validated home. The runner sets SBK_JAVA_HOME only in
       the immutable child environment used for this analytics invocation.
    """
    try:
        required_major = int(version)
    except ValueError:
        log.error("Invalid JDK version '%s', must be a number", version)
        raise ValueError(f"Invalid JDK version: {version}")

    # Use specified folder if provided, otherwise use cache
    if jdk_folder is None:
        cache = _cache_root() / JDK_ARTIFACT.cache_namespace / version
    else:
        cache = jdk_folder / version
        cache.mkdir(parents=True, exist_ok=True)
    
    marker = cache / CACHE_POLICY.completion_marker
    home_file = cache / CACHE_POLICY.home_pointer

    # Helper function to check if a Java installation matches the required version
    def _check_java_home(java_home: Path) -> bool:
        """Check if java_home contains a JDK matching the required version."""
        if not java_home:
            return False
        java_path = _jdk_executable(java_home)
        if not java_path.is_file() or not os.access(java_path, os.X_OK):
            log.debug("Java home %s does not contain bin/java", java_home)
            return False
        major = _java_major_version(java_path)
        log.debug("Java home %s reports major=%s (required=%s)", java_home, major, required_major)
        return major == required_major

    # 1. Check SBK_JAVA_HOME first (highest priority)
    sbk_java_home = os.environ.get(ENVIRONMENT_POLICY.sbk_java_home)
    if sbk_java_home:
        log.info("Checking SBK_JAVA_HOME=%s", sbk_java_home)
        if _check_java_home(Path(sbk_java_home)):
            log.info("SBK_JAVA_HOME points to JDK %s at %s; using it", required_major, sbk_java_home)
            return JdkInstall(home=Path(sbk_java_home))
        else:
            log.warning("SBK_JAVA_HOME is set but does not contain JDK %s", required_major)

    # 2. Check JAVA_HOME (second priority)
    java_home = os.environ.get(ENVIRONMENT_POLICY.java_home)
    if java_home:
        log.info("Checking JAVA_HOME=%s", java_home)
        if _check_java_home(Path(java_home)):
            log.info("JAVA_HOME points to JDK %s at %s; using it", required_major, java_home)
            return JdkInstall(home=Path(java_home))
        else:
            log.warning("JAVA_HOME is set but does not contain JDK %s", required_major)

    # 3. Check java on PATH (third priority)
    from shutil import which
    java_exe = which(JDK_ARTIFACT.primary_executable)
    if java_exe:
        log.info("Checking java on PATH: %s", java_exe)
        java_path = Path(java_exe)
        # Derive JDK home from java executable location
        if java_path.parent.name == LAYOUT_POLICY.executable_directory:
            jdk_home = java_path.parent.parent
            if _check_java_home(jdk_home):
                log.info("java on PATH points to JDK %s at %s; using it", required_major, jdk_home)
                return JdkInstall(home=jdk_home)
        else:
            log.debug("java on PATH is not in a JDK bin directory")
        # Also check the version directly
        major = _java_major_version(java_path)
        if major == required_major:
            log.warning(
                "java on PATH matches version %s but its JDK home cannot be "
                "derived; continuing to a configured cache/download so "
                "SBK_JAVA_HOME is always a valid home",
                required_major,
            )

    # 4. Check specified folder for cached version (fourth priority)
    if jdk_folder and marker.exists() and home_file.exists():
        cached_home = Path(home_file.read_text().strip())
        log.info("Checking cached JDK in specified folder: %s", cached_home)
        if _check_java_home(cached_home):
            log.info("JDK %s found in specified folder %s (cache hit)", required_major, cached_home)
            return JdkInstall(home=cached_home)
        else:
            log.warning("Cached JDK in specified folder does not match version %s; re-downloading", required_major)
            marker.unlink(missing_ok=True)

    # 5. Download JDK to specified folder or cache (fifth priority)
    log.info("No JDK %s found in SBK_JAVA_HOME, JAVA_HOME, PATH, or cache; downloading Temurin JDK %s to %s", required_major, version, cache)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(_cache_lock_path(cache)):
        # Another process may have completed the install while this process
        # waited for the lock.
        if marker.exists() and home_file.exists():
            cached_home = Path(home_file.read_text().strip())
            if _check_java_home(cached_home):
                return JdkInstall(home=cached_home)
        return _install_jdk_locked(version, cache, ssl_verify)


def _install_jdk_locked(
    version: str, cache: Path, ssl_verify: bool | str,
) -> JdkInstall:
    """Download, validate, and atomically publish one managed JDK."""
    stage = _cache_stage_path(cache)
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    url, expected_checksum = _jdk_asset(version, ssl_verify)
    archive = stage / f"jdk-{version}.tar.gz"
    checksum = _download(url, archive, ssl_verify=ssl_verify)
    if checksum.lower() != expected_checksum:
        raise CacheError(
            "downloaded JDK checksum mismatch: "
            f"expected {expected_checksum}, got {checksum.lower()}"
        )

    extract_dir = stage / LAYOUT_POLICY.extracted_directory
    extract_dir.mkdir(parents=True, exist_ok=True)
    top = _extract(archive, extract_dir)

    # locate the actual JDK home (folder containing bin/java)
    home = top
    if not _jdk_executable(home).exists():
        for sub in home.rglob(LAYOUT_POLICY.executable_directory):
            candidate = sub / _jdk_executable(home).name
            if candidate.exists():
                home = sub.parent
                break

    java = _jdk_executable(home)
    if not java.is_file() or not os.access(java, os.X_OK):
        raise RuntimeError(
            f"extracted JDK does not contain executable bin/java under {extract_dir}"
        )

    required_major = int(version)
    actual_major = _java_major_version(java)
    if actual_major != required_major:
        raise CacheError(
            "downloaded JDK failed version validation: "
            f"required major {required_major}, detected {actual_major or 'unknown'}"
        )

    relative_home = home.relative_to(stage)
    final_home = cache / relative_home
    (stage / CACHE_POLICY.home_pointer).write_text(str(final_home.resolve()))
    _write_metadata(
        stage / CACHE_POLICY.metadata_filename,
        dependency=JDK_ARTIFACT.key, version=version,
        source_url=url, sha256=checksum,
        executable=str(_jdk_executable(final_home)),
        detected_major=actual_major,
    )
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    
    (stage / CACHE_POLICY.completion_marker).touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    log.info("JDK %s downloaded and validated at %s", version, final_home)
    return JdkInstall(home=final_home)
