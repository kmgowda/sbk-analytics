#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Resolve shared or managed sbk-charts installations."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from urllib.parse import quote

from ._shared import (
    CACHE_METADATA_POLICY, CACHE_POLICY, DEPENDENCY_POLICY,
    DIAGNOSTIC_FIELDS, DISPLAY_POLICY, ENVIRONMENT_POLICY, LAYOUT_POLICY,
    NETWORK_POLICY, PROVENANCE_POLICY, ChartsInstall, DependencyResolutionError,
    DependencySource, LocalPackageError, _check_version, _command_version,
    _cache_lock, _cache_lock_path, _cache_root, _cache_stage_path,
    _download, _entrypoint_interpreter_ready, _git_details, _local_directory,
    _pip_trusted_host_args, _read_metadata, _release_provenance,
    _relocate_venv_scripts, _require_executable, _run_pip,
    _shared_provenance, _write_metadata,
)
from ..policy import SBK_CHARTS_ARTIFACT

log = logging.getLogger(__name__)


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
    if (
        marker.exists()
        and install.cli.exists()
        and install.python.exists()
        and _entrypoint_interpreter_ready(install.cli)
    ):
        metadata_path = cache / CACHE_POLICY.metadata_filename
        metadata = _read_metadata(metadata_path)
        cached_digest = metadata.get(CACHE_METADATA_POLICY.source_sha256)
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
    final_venv = cache / LAYOUT_POLICY.virtual_environment_directory
    _relocate_venv_scripts(stage_venv, final_venv)
    _write_metadata(
        stage / CACHE_POLICY.metadata_filename,
        {
            CACHE_METADATA_POLICY.dependency: SBK_CHARTS_ARTIFACT.key,
            CACHE_METADATA_POLICY.version: version,
            CACHE_METADATA_POLICY.source_url: source_url,
            CACHE_METADATA_POLICY.executable: str(final_cli),
            CACHE_METADATA_POLICY.source_sha256: source_sha256,
            CACHE_METADATA_POLICY.install_specification: spec,
        },
    )
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    _charts_version(final_cli, require_ready=True)
    (cache / CACHE_POLICY.completion_marker).touch()
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
            metadata={CACHE_METADATA_POLICY.source_sha256: source_sha256},
        ),
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


def _charts_version(cli: Path, *, require_ready: bool = False) -> str | None:
    try:
        result = subprocess.run(
            [str(cli), *SBK_CHARTS_ARTIFACT.version_arguments],
            capture_output=True, text=True,
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
            SBK_CHARTS_ARTIFACT.version_pattern or "",
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
            DEPENDENCY_POLICY.python_metadata_script_template.format(
                distribution=SBK_CHARTS_ARTIFACT.distribution_name
            ),
        ],
        DEPENDENCY_POLICY.generic_version_pattern,
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


def inspect_shared_sbk_charts(
    folder: Path | None = None, *, executable: Path | None = None,
) -> dict:
    """Describe shared sbk-charts paths without starting or modifying them."""
    configured = executable or folder
    result: dict = {
        DIAGNOSTIC_FIELDS.configured_location: (
            str(configured) if configured is not None else None
        ),
        DIAGNOSTIC_FIELDS.read_only: True,
        DIAGNOSTIC_FIELDS.install_performed: False,
        DIAGNOSTIC_FIELDS.valid: False,
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
        result[DIAGNOSTIC_FIELDS.error] = str(exc)
        return result
    for cli, layout in candidates:
        if not cli.is_file():
            continue
        ready = os.access(cli, os.X_OK)
        revision, dirty = _git_details(provenance_root)
        result.update({
            DIAGNOSTIC_FIELDS.valid: ready,
            DIAGNOSTIC_FIELDS.layout: layout,
            DIAGNOSTIC_FIELDS.resolved_location: str(root),
            DIAGNOSTIC_FIELDS.executable: str(cli),
            DIAGNOSTIC_FIELDS.executable_ready: ready,
            DIAGNOSTIC_FIELDS.revision: revision,
            DIAGNOSTIC_FIELDS.dirty: dirty,
        })
        if not ready:
            result[DIAGNOSTIC_FIELDS.error] = (
                f"{SBK_CHARTS_ARTIFACT.display_name} executable is not "
                f"executable: {cli}"
            )
        return result
    result[DIAGNOSTIC_FIELDS.error] = (
        "no supported executable sbk-charts command found"
    )
    return result
