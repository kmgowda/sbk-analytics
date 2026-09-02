#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Stable dependency-resolution API grouped by artifact implementation."""
from __future__ import annotations

# Imported modules remain available for compatibility with existing diagnostic
# and test tooling that patches native process/network helpers on this facade.
from ._shared import requests, subprocess, venv
from ._shared import (
    ChartsInstall, DependencySource, JdkInstall, SbkInstall, SourceProvenance,
    _cache_lock, _cache_lock_path, _cache_root, _cache_stage_path,
    _charts_local_candidates, _charts_version, _command_version, _download,
    _entrypoint_interpreter_ready, _extract, _gh_release, _git_details,
    _jdk_executable, _local_directory, _read_metadata, _release_provenance,
    _relocate_venv_scripts, _require_executable, _run_pip,
    _sbk_local_candidates, _shared_provenance,
    cache_root, inspect_shared_sbk, inspect_shared_sbk_charts,
    managed_metadata, resolve_local_sbk, resolve_local_sbk_charts,
)
from .charts import ensure_sbk_charts
from .jdk import (
    _candidate_jdk_homes, _install_jdk_locked, _java_major_version,
    _jdk_asset, _jdk_platform, ensure_jdk, find_existing_jdk,
)
from .sbk import _ensure_sbk_locked, ensure_sbk

__all__ = (
    "ChartsInstall", "DependencySource", "JdkInstall", "SbkInstall",
    "SourceProvenance", "cache_root", "ensure_jdk", "ensure_sbk",
    "ensure_sbk_charts", "find_existing_jdk", "inspect_shared_sbk",
    "inspect_shared_sbk_charts", "managed_metadata", "resolve_local_sbk",
    "resolve_local_sbk_charts",
)
