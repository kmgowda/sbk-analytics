#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Resolve and validate local or managed Temurin JDKs."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

from ._shared import (
    ARCHIVE_POLICY, CACHE_METADATA_POLICY, CACHE_POLICY, DEPENDENCY_POLICY,
    ENVIRONMENT_POLICY, LAYOUT_POLICY, NETWORK_POLICY, CacheError,
    DependencyResolutionError, JdkInstall, _cache_lock, _cache_lock_path,
    _cache_root, _cache_stage_path, _download, _extract, _write_metadata,
)
from ..policy import JDK_ARTIFACT

log = logging.getLogger(__name__)

_JDK_VERSION_RE = re.compile(JDK_ARTIFACT.version_pattern or "")


def _java_major_version(java_path: Path) -> int | None:
    """Return the major version number reported by ``<java_path> -version``,
    or ``None`` if it cannot be determined.

    Handles both modern Java (``25.0.3``, ``21.0.5``, ``17.0.10``) and the
    legacy ``1.8.0_xxx`` form (where the major version is the second part).
    """
    try:
        proc = subprocess.run(
            [str(java_path), *JDK_ARTIFACT.version_arguments],
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
    machine = os.uname().machine
    arch = (
        DEPENDENCY_POLICY.jdk_x86_64_architecture
        if machine in DEPENDENCY_POLICY.jdk_x86_64_aliases
        else machine
    )
    try:
        os_name = dict(DEPENDENCY_POLICY.jdk_platform_names)[sys.platform]
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
        package = assets[0][DEPENDENCY_POLICY.jdk_metadata_binary_field][
            DEPENDENCY_POLICY.jdk_metadata_package_field
        ]
        download_url = str(
            package[DEPENDENCY_POLICY.jdk_metadata_link_field]
        )
        checksum = str(
            package[DEPENDENCY_POLICY.jdk_metadata_checksum_field]
        ).lower()
    except (requests.RequestException, ValueError, IndexError, KeyError, TypeError) as exc:
        raise DependencyResolutionError(
            f"could not resolve checksum-verified Temurin JDK {version} metadata"
        ) from exc
    if not re.fullmatch(CACHE_METADATA_POLICY.sha256_pattern, checksum):
        raise DependencyResolutionError(
            f"Temurin JDK {version} metadata contains an invalid SHA-256"
        )
    if not download_url.startswith(NETWORK_POLICY.https_prefix):
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
    archive = stage / DEPENDENCY_POLICY.jdk_archive_name_template.format(
        version=version,
        archive_suffix=ARCHIVE_POLICY.preferred_tar_suffix,
    )
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
        {
            CACHE_METADATA_POLICY.dependency: JDK_ARTIFACT.key,
            CACHE_METADATA_POLICY.version: version,
            CACHE_METADATA_POLICY.source_url: url,
            CACHE_METADATA_POLICY.sha256: checksum,
            CACHE_METADATA_POLICY.executable: str(_jdk_executable(final_home)),
            CACHE_METADATA_POLICY.detected_major: actual_major,
        },
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


def _jdk_executable(home: Path) -> Path:
    return (
        home / LAYOUT_POLICY.executable_directory
        / JDK_ARTIFACT.primary_executable
    )
