"""Parse simple key=value `.env`-style properties files.

Recognised keys (case-insensitive; dots / underscores / dashes equivalent):

    sbk.url                -> GitHub repo URL for SBK
                              (e.g. https://github.com/kmgowda/SBK)
    sbk.version            -> SBK release tag on that repo
    sbk.jdk.version        -> JDK major version required by that SBK release
                              (default: 25). The orchestrator first looks for
                              an already-installed JDK whose major version
                              matches (via SBK_JAVA_HOME / JAVA_HOME / `java`
                              on PATH), and only downloads Temurin of this
                              major version if none is found.
    sbk-charts.url         -> GitHub repo URL for sbk-charts
                              (e.g. https://github.com/kmgowda/sbk-charts)
    sbk-charts.version     -> sbk-charts release tag on that repo

The URLs may be either ``https://github.com/<owner>/<repo>`` or just
``<owner>/<repo>``. If a URL is missing, a sensible default is used:

    sbk.url        -> https://github.com/kmgowda/SBK
    sbk-charts.url -> https://github.com/kmgowda/sbk-charts
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_SBK_URL = "https://github.com/kmgowda/SBK"
DEFAULT_SBK_CHARTS_URL = "https://github.com/kmgowda/sbk-charts"
DEFAULT_SBK_JDK_VERSION = "25"


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


@dataclass(frozen=True)
class Versions:
    sbk: str               # SBK release tag, e.g. "10.0"
    sbk_charts: str        # sbk-charts release tag, e.g. "4.26.6.1"
    sbk_url: str           # canonical SBK repo URL
    sbk_charts_url: str    # canonical sbk-charts repo URL
    sbk_jdk: str           # required JDK major version, e.g. "25"

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

    return Versions(
        sbk=_get("sbk.version", "sbk_version"),
        sbk_charts=_get(
            "sbk.charts.version", "sbk_charts_version", "sbkcharts.version"
        ),
        sbk_url=_normalise_repo_url(sbk_url_raw),
        sbk_charts_url=_normalise_repo_url(sbk_charts_url_raw),
        sbk_jdk=sbk_jdk,
    )
