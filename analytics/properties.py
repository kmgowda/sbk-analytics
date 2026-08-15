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
    downloads.folder       -> Shared local folder for downloaded SBK and
                              sbk-charts installations (default: ./.sbk)
    sbk.jdk.version        -> JDK major version required by that SBK release
                              (default: 25). The orchestrator first looks for
                              an already-installed JDK whose major version
                              matches (via SBK_JAVA_HOME / JAVA_HOME / `java`
                              on PATH), and only downloads Temurin of this
                              major version if none is found.
    sbk.jdk.folder         -> Local folder for JDK installation (default: ./.jdk)
    ssl.verify             -> Enable SSL verification for downloads (default: true)
    sbk-charts.url         -> GitHub repo URL for sbk-charts
                              (e.g. https://github.com/kmgowda/sbk-charts)
    sbk-charts.version     -> sbk-charts release tag on that repo

The URLs may be either ``https://github.com/<owner>/<repo>`` or just
``<owner>/<repo>``. If a URL is missing, a sensible default is used:

    sbk.url        -> https://github.com/kmgowda/SBK
    sbk-charts.url -> https://github.com/kmgowda/sbk-charts

The folder paths may be absolute or relative to the properties file location.
Relative paths are resolved against the properties file's directory.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_SBK_URL = "https://github.com/kmgowda/SBK"
DEFAULT_SBK_CHARTS_URL = "https://github.com/kmgowda/sbk-charts"
DEFAULT_SBK_JDK_VERSION = "25"
DEFAULT_DOWNLOADS_FOLDER = "./.sbk"
DEFAULT_JDK_FOLDER = "./.jdk"
DEFAULT_SSL_VERIFY = "true"


def _norm(key: str) -> str:
    return key.strip().lower().replace("-", ".").replace("_", ".")


def _normalise_repo_url(url: str) -> str:
    """Accept either a full GitHub URL or an `owner/repo` shorthand and
    return a canonical ``https://github.com/<owner>/<repo>`` URL.
    """
    s = url.strip().rstrip("/")
    if s.endswith(".git"):
        s = s[:-4]
    if "://" not in s:
        # treat as owner/repo
        parts = [p for p in s.split("/") if p]
        if len(parts) == 2:
            return f"https://github.com/{parts[0]}/{parts[1]}"
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
    sbk_url: str           # canonical SBK repo URL
    sbk_charts_url: str    # canonical sbk-charts repo URL
    sbk_jdk: str           # required JDK major version, e.g. "25"
    downloads_folder: Path  # shared folder for downloaded SBK and sbk-charts installations
    jdk_folder: Path       # local folder for JDK installation
    ssl_verify: bool       # enable SSL verification for downloads

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

    sbk_url_raw = _get("sbk.url", "sbk_url", default=DEFAULT_SBK_URL)
    sbk_charts_url_raw = _get(
        "sbk.charts.url", "sbk_charts_url", "sbkcharts.url",
        default=DEFAULT_SBK_CHARTS_URL,
    )
    sbk_jdk = _get(
        "sbk.jdk.version", "sbk_jdk_version", "jdk.version", "jdk_version",
        default=DEFAULT_SBK_JDK_VERSION,
    ).strip()
    
    downloads_folder_raw = _get(
        "downloads.folder", "downloads_folder",
        default=DEFAULT_DOWNLOADS_FOLDER,
    )
    jdk_folder_raw = _get(
        "sbk.jdk.folder", "sbk_jdk_folder", "jdk.folder", "jdk_folder",
        default=DEFAULT_JDK_FOLDER,
    )
    
    ssl_verify_raw = _get(
        "ssl.verify", "ssl_verify", "verify", "verify.ssl",
        default=DEFAULT_SSL_VERIFY,
    )
    ssl_verify = ssl_verify_raw.lower() in ("1", "true", "yes", "on")

    return Versions(
        sbk=_get("sbk.version", "sbk_version"),
        sbk_charts=_get(
            "sbk.charts.version", "sbk_charts_version", "sbkcharts.version"
        ),
        sbk_url=_normalise_repo_url(sbk_url_raw),
        sbk_charts_url=_normalise_repo_url(sbk_charts_url_raw),
        sbk_jdk=sbk_jdk,
        downloads_folder=_resolve_folder(downloads_folder_raw, p),
        jdk_folder=_resolve_folder(jdk_folder_raw, p),
        ssl_verify=ssl_verify,
    )
