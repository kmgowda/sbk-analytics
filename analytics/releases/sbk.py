#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
from __future__ import annotations

"""Resolve shared or managed SBK distributions."""

import logging
import os
import shutil
from pathlib import Path
from urllib.parse import urlparse

from ._shared import (
    ARCHIVE_POLICY, CACHE_METADATA_POLICY, CACHE_POLICY, DEPENDENCY_POLICY,
    LAYOUT_POLICY, NETWORK_POLICY, CacheError, DependencySource, SbkInstall,
    _cache_lock, _cache_lock_path, _cache_root, _cache_stage_path, _download,
    _extract, _gh_release, _read_metadata, _release_provenance,
    _require_executable, _write_metadata, resolve_local_sbk,
)
from ..policy import SBK_ARTIFACT

log = logging.getLogger(__name__)

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
    assets = rel.get(NETWORK_POLICY.release_assets_field) or []
    # Prefer a top-level distribution asset named like 'sbk-<ver>.tar' (not sbk-gem-yal-*)
    candidates = []
    for a in assets:
        n = a[NETWORK_POLICY.release_asset_name_field].lower()
        if not n.startswith(SBK_ARTIFACT.key):
            continue
        if not n.endswith(ARCHIVE_POLICY.release_suffixes):
            continue
        # de-prioritise sub-distros like sbk-gem-yal-X.tar
        score = 0 if n.startswith(("sbk-" + version.lower(), f"sbk-{version}")) else 1
        if "gem" in n or "yal" in n or "sbm" in n:
            score += ARCHIVE_POLICY.secondary_asset_penalty
        candidates.append((score, a))

    if not candidates:
        raise RuntimeError(
            f"no SBK distribution archive found in release {version}; "
            "assets: "
            f"{[a[NETWORK_POLICY.release_asset_name_field] for a in assets]}"
        )
    candidates.sort(key=lambda x: x[0])
    asset = candidates[0][1]
    url = asset[NETWORK_POLICY.release_asset_url_field]
    asset_name = asset[NETWORK_POLICY.release_asset_name_field]
    log.info("selected SBK asset: %s", asset_name)

    archive = stage / Path(urlparse(url).path).name
    if not archive.exists():
        checksum = _download(url, archive, ssl_verify=ssl_verify)
    else:
        checksum = None
    # GitHub.com exposes `digest` for release assets. Older GitHub Enterprise
    # versions may omit it, in which case metadata still records our checksum.
    expected_digest = asset.get(NETWORK_POLICY.release_asset_digest_field)
    if expected_digest and expected_digest.startswith(
        NETWORK_POLICY.sha256_digest_prefix
    ):
        expected_sha256 = expected_digest.split(":", 1)[1].lower()
        if checksum != expected_sha256:
            raise CacheError(
                f"SBK asset checksum mismatch for {asset_name}: "
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
                f.chmod(
                    f.stat().st_mode | ARCHIVE_POLICY.executable_mode_mask
                )
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
        {
            CACHE_METADATA_POLICY.dependency: SBK_ARTIFACT.key,
            CACHE_METADATA_POLICY.version: version,
            CACHE_METADATA_POLICY.source_url: url,
            CACHE_METADATA_POLICY.asset: asset_name,
            CACHE_METADATA_POLICY.sha256: checksum,
            CACHE_METADATA_POLICY.executables: {
                executable: str(
                    final_home / LAYOUT_POLICY.executable_directory / executable
                )
                for executable in SBK_ARTIFACT.executables
            },
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
            metadata={
                CACHE_METADATA_POLICY.asset: asset_name,
                CACHE_METADATA_POLICY.sha256: checksum,
            },
        ),
    )
