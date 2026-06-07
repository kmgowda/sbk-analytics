"""Parse simple key=value `.env`-style properties files.

Recognised keys (case-insensitive, dots/underscores/dashes equivalent):
    sbk.version            -> SBK release tag on github.com/kmgowda/SBK
    sbk-charts.version     -> sbk-charts release tag on github.com/kmgowda/sbk-charts
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def _norm(key: str) -> str:
    return key.strip().lower().replace("-", ".").replace("_", ".")


@dataclass(frozen=True)
class Versions:
    sbk: str
    sbk_charts: str


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

    def _get(*aliases: str) -> str:
        for a in aliases:
            n = _norm(a)
            if n in data and data[n]:
                return data[n]
        raise KeyError(
            f"missing required property; expected one of {aliases} in {p}"
        )

    return Versions(
        sbk=_get("sbk.version", "sbk_version"),
        sbk_charts=_get(
            "sbk.charts.version", "sbk_charts_version", "sbkcharts.version"
        ),
    )
