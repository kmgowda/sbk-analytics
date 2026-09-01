#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Parse simple key=value `.env`-style properties files.

Recognised keys (case-insensitive; dots / underscores / dashes equivalent):

    sbk.url                -> GitHub repo URL for SBK
                              (e.g. https://github.com/kmgowda/SBK)
    sbk.version            -> SBK release tag on that repo
    sbk.local.folder       -> Optional ready-to-run local SBK distribution or
                              built source checkout; bypasses cache/download
    downloads.folder       -> Shared local folder for downloaded SBK and
                              sbk-charts installations (default: ./.sbk)
    sbk.jdk.version        -> JDK major version required by that SBK release
                              (default: 25). The orchestrator first looks for
                              an already-installed JDK whose major version
                              matches (via SBK_JAVA_HOME / JAVA_HOME / `java`
                              on PATH), and only downloads Temurin of this
                              major version if none is found.
    sbk.jdk.folder         -> Local folder for JDK installation (default: ./.jdk)
    ssl.verify             -> Enable SSL verification for downloads (default: false)
    ssl.ca.bundle          -> Optional PEM CA bundle used when verification is enabled
    sbk-charts.url         -> GitHub repo URL for sbk-charts
                              (e.g. https://github.com/kmgowda/sbk-charts)
    sbk-charts.version     -> sbk-charts release tag on that repo
    sbk-charts.sha256      -> Optional SHA-256 for its managed tag archive;
                              verifies the source and removes the Git requirement
    sbk-charts.local.folder
                           -> Optional ready-to-run local sbk-charts checkout
                              or environment; bypasses cache/download
    sbk-charts.local.executable
                           -> Optional direct path to a local sbk-charts command
    sbk.version.policy     -> local version handling: warn | exact | ignore
    sbk-charts.version.policy
                           -> local/conda version handling: warn | exact | ignore

The URLs may be either ``https://github.com/<owner>/<repo>`` or just
``<owner>/<repo>``. If a URL is missing, a sensible default is used:

    sbk.url        -> https://github.com/kmgowda/SBK
    sbk-charts.url -> https://github.com/kmgowda/sbk-charts

The folder paths may be absolute or relative to the properties file location.
Relative paths are resolved against the properties file's directory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .policy import RUNTIME_POLICY, SBK_ARTIFACT, SBK_CHARTS_ARTIFACT


DEPENDENCY_POLICY = RUNTIME_POLICY.dependencies
CACHE_POLICY = RUNTIME_POLICY.cache
CONFIGURATION_POLICY = RUNTIME_POLICY.configuration
PROPERTIES_POLICY = RUNTIME_POLICY.properties
LAYOUT_POLICY = RUNTIME_POLICY.dependency_layout


def _norm(key: str) -> str:
    return key.strip().lower().replace("-", ".").replace("_", ".")


def _normalise_repo_url(url: str) -> str:
    """Accept either a full GitHub URL or an `owner/repo` shorthand and
    return a canonical ``https://github.com/<owner>/<repo>`` URL.
    """
    s = url.strip().rstrip("/")
    if s.endswith(LAYOUT_POLICY.git_url_suffix):
        s = s[:-len(LAYOUT_POLICY.git_url_suffix)]
    if "://" not in s:
        # treat as owner/repo
        parts = [p for p in s.split("/") if p]
        if len(parts) == 2:
            return (
                f"{RUNTIME_POLICY.network.github_web_url}/"
                f"{parts[0]}/{parts[1]}"
            )
        raise ValueError(
            f"expected '<owner>/<repo>' or a full URL, got: {url!r}"
        )
    return s


def _owner_repo(url: str) -> str:
    """Return ``owner/repo`` for a canonical GitHub repo URL."""
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot extract owner/repo from URL: {url!r}")
    return f"{parts[0]}/{parts[1]}"


def _resolve_folder(folder: str, properties_file: Path) -> Path:
    """Resolve a folder path relative to the properties file location.
    
    If the path is absolute, return it as-is. If relative, resolve it
    against the directory containing the properties file.
    """
    p = Path(folder).expanduser()
    if p.is_absolute():
        return p
    return properties_file.parent / p


@dataclass(frozen=True)
class Versions:
    sbk: str               # SBK release tag, e.g. "10.0"
    sbk_charts: str        # sbk-charts release tag, e.g. "4.26.6.2"
    sbk_charts_sha256: str | None  # optional managed source archive digest
    sbk_url: str           # canonical SBK repo URL
    sbk_charts_url: str    # canonical sbk-charts repo URL
    sbk_jdk: str           # required JDK major version, e.g. "25"
    downloads_folder: Path | None  # explicitly configured shared download cache
    sbk_local_folder: Path | None  # optional ready-to-run local SBK folder
    sbk_charts_local_folder: Path | None  # optional local sbk-charts folder
    sbk_charts_local_executable: Path | None  # optional direct charts command
    jdk_folder: Path       # local folder for JDK installation
    ssl_verify: bool       # enable SSL verification for downloads
    ssl_ca_bundle: Path | None
    sbk_version_policy: str
    sbk_charts_version_policy: str

    @property
    def sbk_repo(self) -> str:
        """``owner/repo`` for the SBK repository."""
        return _owner_repo(self.sbk_url)

    @property
    def sbk_charts_repo(self) -> str:
        """``owner/repo`` for the sbk-charts repository."""
        return _owner_repo(self.sbk_charts_url)


def parse_properties(path: str | Path) -> Versions:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"properties file not found: {p}")

    data: dict[str, str] = {}
    for lineno, raw in enumerate(p.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            raise ValueError(f"{p}:{lineno}: expected key=value, got: {raw!r}")
        k, v = line.split("=", 1)
        data[_norm(k)] = v.strip().strip('"').strip("'")

    def _get(*aliases: str, default: str | None = None) -> str:
        for a in aliases:
            n = _norm(a)
            if n in data and data[n]:
                return data[n]
        if default is not None:
            return default
        raise KeyError(
            f"missing required property; expected one of {aliases} in {p}"
        )

    def _get_optional(*aliases: str) -> str | None:
        """Return the first non-empty optional property, or ``None``."""
        for alias in aliases:
            value = data.get(_norm(alias))
            if value:
                return value
        return None

    sbk_url_raw = _get(
        *PROPERTIES_POLICY.sbk_url_keys,
        default=SBK_ARTIFACT.repository_url,
    )
    sbk_charts_url_raw = _get(
        *PROPERTIES_POLICY.charts_url_keys,
        default=SBK_CHARTS_ARTIFACT.repository_url,
    )
    sbk_jdk = _get(
        *PROPERTIES_POLICY.jdk_version_keys,
        default=DEPENDENCY_POLICY.default_jdk_version,
    ).strip()
    
    downloads_folder_raw = _get_optional(
        *PROPERTIES_POLICY.downloads_folder_keys
    )
    sbk_local_folder_raw = _get_optional(
        *PROPERTIES_POLICY.sbk_local_folder_keys
    )
    sbk_charts_local_folder_raw = _get_optional(
        *PROPERTIES_POLICY.charts_local_folder_keys,
    )
    sbk_charts_local_executable_raw = _get_optional(
        *PROPERTIES_POLICY.charts_local_executable_keys,
    )
    sbk_charts_sha256 = _get_optional(
        *PROPERTIES_POLICY.charts_sha256_keys,
    )
    if sbk_charts_sha256 is not None:
        sbk_charts_sha256 = sbk_charts_sha256.strip().lower()
        if not re.fullmatch(
            RUNTIME_POLICY.cache_metadata.sha256_pattern,
            sbk_charts_sha256,
        ):
            raise ValueError(
                "sbk-charts.sha256 must contain exactly 64 hexadecimal characters"
            )
    jdk_folder_raw = _get(
        *PROPERTIES_POLICY.jdk_folder_keys,
        default=CACHE_POLICY.default_jdk_folder,
    )
    
    ssl_verify_raw = _get(
        *PROPERTIES_POLICY.ssl_verify_keys,
        default=str(DEPENDENCY_POLICY.default_ssl_verify).lower(),
    )
    bool_values = {
        **{token: True for token in CONFIGURATION_POLICY.true_tokens},
        **{token: False for token in CONFIGURATION_POLICY.false_tokens},
    }
    try:
        ssl_verify = bool_values[ssl_verify_raw.strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"ssl.verify must be true or false, got {ssl_verify_raw!r}"
        ) from exc
    ssl_ca_bundle_raw = _get_optional(*PROPERTIES_POLICY.ssl_ca_bundle_keys)

    def _policy(*aliases: str) -> str:
        value = _get(
            *aliases, default=DEPENDENCY_POLICY.default_version_policy
        ).strip().lower()
        if value not in DEPENDENCY_POLICY.version_policies:
            raise ValueError(
                f"{aliases[0]} must be one of "
                f"{DEPENDENCY_POLICY.version_policies}, got {value!r}"
            )
        return value

    return Versions(
        # Versions are conditionally required by the CLI only for managed
        # resolution. This permits a minimal local-only file or CLI overrides.
        sbk=_get(*PROPERTIES_POLICY.sbk_version_keys, default=""),
        sbk_charts=_get(
            *PROPERTIES_POLICY.charts_version_keys,
            default="",
        ),
        sbk_charts_sha256=sbk_charts_sha256,
        sbk_url=_normalise_repo_url(sbk_url_raw),
        sbk_charts_url=_normalise_repo_url(sbk_charts_url_raw),
        sbk_jdk=sbk_jdk,
        downloads_folder=(
            _resolve_folder(downloads_folder_raw, p)
            if downloads_folder_raw is not None else None
        ),
        sbk_local_folder=(
            _resolve_folder(sbk_local_folder_raw, p)
            if sbk_local_folder_raw is not None
            else None
        ),
        sbk_charts_local_folder=(
            _resolve_folder(sbk_charts_local_folder_raw, p)
            if sbk_charts_local_folder_raw is not None
            else None
        ),
        sbk_charts_local_executable=(
            _resolve_folder(sbk_charts_local_executable_raw, p)
            if sbk_charts_local_executable_raw is not None else None
        ),
        jdk_folder=_resolve_folder(jdk_folder_raw, p),
        ssl_verify=ssl_verify,
        ssl_ca_bundle=(
            _resolve_folder(ssl_ca_bundle_raw, p)
            if ssl_ca_bundle_raw is not None else None
        ),
        sbk_version_policy=_policy(
            *PROPERTIES_POLICY.sbk_version_policy_keys
        ),
        sbk_charts_version_policy=_policy(
            *PROPERTIES_POLICY.charts_version_policy_keys
        ),
    )
