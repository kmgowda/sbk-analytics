"""Download and cache SBK + sbk-charts release artifacts from GitHub.

SBK is shipped as a `.tar` (or `.tar.gz`/`.zip`) containing a `bin/` directory with
`sbk-yal` and `sbk-gem-yal` shell scripts. sbk-charts is a Python package, installed
into an isolated venv via pip from the GitHub release tag.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import time
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


def _cache_root() -> Path:
    root = os.environ.get("SBK_ANALYTICS_CACHE")
    if root:
        return Path(root)
    return Path.home() / ".cache" / "sbk-analytics"


@dataclass
class SbkInstall:
    home: Path  # extracted SBK distribution root (contains bin/)

    @property
    def sbk_yal(self) -> Path:
        return self.home / "bin" / "sbk-yal"

    @property
    def sbk_gem_yal(self) -> Path:
        return self.home / "bin" / "sbk-gem-yal"


@dataclass
class ChartsInstall:
    venv_dir: Path  # python venv with sbk-charts installed

    @property
    def cli(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "sbk-charts.exe"
        return self.venv_dir / "bin" / "sbk-charts"

    @property
    def python(self) -> Path:
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"


# ---------- helpers ----------


def _gh_release(repo: str, tag: str) -> dict:
    """Fetch release metadata from GitHub for a given tag."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        raise RuntimeError(f"GitHub release not found: {repo}@{tag}")
    r.raise_for_status()
    return r.json()


def _download(url: str, dest: Path, *, max_attempts: int = 6) -> None:
    """Download `url` to `dest`, resuming via HTTP Range if .part already exists."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        offset = tmp.stat().st_size if tmp.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        log.info(
            "downloading %s -> %s (attempt %d, offset=%d)",
            url, dest, attempt, offset,
        )
        try:
            with requests.get(url, stream=True, timeout=120, headers=headers) as r:
                if offset and r.status_code == 200:
                    # server ignored Range; restart from scratch
                    tmp.unlink(missing_ok=True)
                    offset = 0
                elif r.status_code not in (200, 206):
                    r.raise_for_status()
                mode = "ab" if offset else "wb"
                with tmp.open(mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
            tmp.replace(dest)
            return
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout) as e:
            last_err = e
            wait = min(2 ** attempt, 30)
            log.warning(
                "download interrupted (%s); retrying in %ds (got %d bytes so far)",
                e.__class__.__name__, wait, tmp.stat().st_size if tmp.exists() else 0,
            )
            time.sleep(wait)
    raise RuntimeError(f"failed to download {url} after {max_attempts} attempts") from last_err


def _extract(archive: Path, dest: Path) -> Path:
    """Extract archive into dest/, return the (single) top-level dir inside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
        with tarfile.open(archive) as tf:
            tf.extractall(dest)
    else:
        raise RuntimeError(f"unsupported archive type: {archive.name}")

    # locate top-level dir
    entries = [p for p in dest.iterdir() if p.is_dir()]
    if len(entries) == 1:
        return entries[0]
    return dest


# ---------- SBK ----------


def ensure_sbk(version: str, repo: str = "kmgowda/SBK") -> SbkInstall:
    """Ensure SBK <version> is downloaded + extracted, return install info."""
    cache = _cache_root() / "sbk" / version
    marker = cache / ".ok"
    home_file = cache / ".home"

    if marker.exists() and home_file.exists():
        home = Path(home_file.read_text().strip())
        # Validate that the extracted distribution still has the binaries.
        if (home / "bin" / "sbk-yal").exists():
            log.info("SBK %s already installed at %s (cache hit)", version, home)
            return SbkInstall(home=home)
        log.warning(
            "SBK %s cache marker exists but binaries missing at %s; re-installing",
            version, home,
        )
        marker.unlink(missing_ok=True)

    rel = _gh_release(repo, version)
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

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(urlparse(url).path).name
    if not archive.exists():
        _download(url, archive)

    extract_dir = cache / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    top = _extract(archive, extract_dir)

    # Find the directory that actually contains bin/sbk-yal
    home = top
    if not (home / "bin" / "sbk-yal").exists():
        for sub in home.rglob("bin"):
            if (sub / "sbk-yal").exists():
                home = sub.parent
                break

    # make scripts executable
    bindir = home / "bin"
    if bindir.is_dir():
        for f in bindir.iterdir():
            try:
                f.chmod(f.stat().st_mode | 0o111)
            except OSError:
                pass

    home_file.write_text(str(home))
    marker.touch()
    # Free disk: the ~1+ GB archive is no longer needed once extracted.
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    log.info("SBK %s ready at %s", version, home)
    return SbkInstall(home=home)


# ---------- sbk-charts ----------


def ensure_sbk_charts(
    version: str,
    repo_url: str = "https://github.com/kmgowda/sbk-charts",
) -> ChartsInstall:
    """Ensure sbk-charts <version> is installed in a dedicated venv."""
    cache = _cache_root() / "sbk-charts" / version
    venv_dir = cache / "venv"
    marker = cache / ".ok"

    install = ChartsInstall(venv_dir=venv_dir)
    if marker.exists() and install.cli.exists() and install.python.exists():
        log.info(
            "sbk-charts %s already installed at %s (cache hit)", version, venv_dir
        )
        return install
    if marker.exists():
        log.warning(
            "sbk-charts %s cache marker exists but venv is incomplete; re-installing",
            version,
        )
        marker.unlink(missing_ok=True)

    cache.mkdir(parents=True, exist_ok=True)

    log.info("creating venv for sbk-charts %s at %s", version, venv_dir)
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(venv_dir)

    # Install from the GitHub tag (release source tarball)
    # pip wants 'git+<url>.git@<ref>' for VCS installs
    pip_url = repo_url.rstrip("/")
    if not pip_url.endswith(".git"):
        pip_url = pip_url + ".git"
    spec = f"git+{pip_url}@{version}"

    cmd = [
        str(install.python),
        "-m",
        "pip",
        "install",
        "--quiet",
        "--upgrade",
        "pip",
    ]
    subprocess.run(cmd, check=True)
    cmd = [str(install.python), "-m", "pip", "install", spec]
    log.info("installing %s", spec)
    subprocess.run(cmd, check=True)

    if not install.cli.exists():
        # some versions expose differently named entry points
        bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        candidates = list(bindir.glob("sbk-charts*")) + list(bindir.glob("sb-charts*"))
        if candidates:
            install = ChartsInstall(venv_dir=venv_dir)
            # rewrite expected path; cli property derived dynamically
            log.warning(
                "sbk-charts CLI not at expected path; found: %s",
                [c.name for c in candidates],
            )
        else:
            raise RuntimeError(
                f"sbk-charts installed but no CLI script found under {bindir}"
            )

    marker.touch()
    return install
