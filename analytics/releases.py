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

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import venv
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)


def _cache_root() -> Path:
    root = os.environ.get("SBK_ANALYTICS_CACHE")
    if root:
        return Path(root)
    return Path.home() / ".cache" / "sbk-analytics"


def _use_specified_cache(specified_folder: Path) -> Path:
    """Use the specified folder from sbk-config.env if provided, otherwise use cache."""
    if specified_folder and specified_folder != Path("./.jdk") and specified_folder != Path("./.sbk"):
        return specified_folder
    return _cache_root()


class DependencySource(str, Enum):
    """How a resolved dependency was obtained for this invocation."""

    LOCAL = "LOCAL"
    MANAGED_CACHE = "MANAGED_CACHE"
    DOWNLOADED = "DOWNLOADED"
    CONDA = "CONDA"


@dataclass
class SbkInstall:
    home: Path  # selected SBK distribution root (contains bin/)
    source: DependencySource = DependencySource.MANAGED_CACHE
    _sbk_yal: Path | None = None
    _sbk_gem_yal: Path | None = None

    @property
    def sbk_yal(self) -> Path:
        return self._sbk_yal or self.home / "bin" / "sbk-yal"

    @property
    def sbk_gem_yal(self) -> Path | None:
        if self._sbk_gem_yal is not None:
            return self._sbk_gem_yal
        default = self.home / "bin" / "sbk-gem-yal"
        return default if default.is_file() else None


@dataclass
class JdkInstall:
    home: Path  # extracted JDK home (contains bin/java)

    @property
    def java(self) -> Path:
        if os.name == "nt":
            return self.home / "bin" / "java.exe"
        return self.home / "bin" / "java"


@dataclass
class ChartsInstall:
    venv_dir: Path  # selected sbk-charts checkout or environment root
    source: DependencySource = DependencySource.MANAGED_CACHE
    _cli: Path | None = None
    _python: Path | None = None

    @property
    def cli(self) -> Path:
        if self._cli is not None:
            return self._cli
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "sbk-charts.exe"
        return self.venv_dir / "bin" / "sbk-charts"

    @property
    def python(self) -> Path:
        if self._python is not None:
            return self._python
        if os.name == "nt":
            return self.venv_dir / "Scripts" / "python.exe"
        return self.venv_dir / "bin" / "python"


# ---------- helpers ----------


def _local_directory(folder: Path, dependency: str) -> Path:
    """Return a canonical local dependency directory or fail clearly.

    An explicitly configured local folder is authoritative: callers must not
    fall back to a download when this validation fails.
    """
    try:
        root = folder.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(
            f"{dependency} local folder does not exist: {folder}"
        ) from exc
    if not root.is_dir():
        raise RuntimeError(f"{dependency} local folder is not a directory: {root}")
    return root


def _require_executable(path: Path, dependency: str) -> Path:
    """Validate a local command without modifying its permissions."""
    if not path.is_file():
        raise RuntimeError(f"{dependency} executable is missing: {path}")
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"{dependency} executable is not executable: {path}")
    return path


def resolve_local_sbk(folder: Path, *, require_gem: bool = False) -> SbkInstall:
    """Resolve a ready-to-run SBK distribution or built source checkout.

    Supported roots contain either ``bin/sbk-yal`` (a distribution) or
    ``build/install/sbk/bin/sbk-yal`` (a Gradle ``installDist`` checkout).
    The bounded list deliberately avoids selecting stale artifacts via a
    recursive filesystem search.
    """
    root = _local_directory(folder, "SBK")
    homes = (root, root / "build" / "install" / "sbk")
    for home in homes:
        sbk_yal = home / "bin" / "sbk-yal"
        if not sbk_yal.is_file():
            continue
        sbk_gem_yal = home / "bin" / "sbk-gem-yal"
        resolved_gem = None
        if sbk_gem_yal.is_file() and os.access(sbk_gem_yal, os.X_OK):
            resolved_gem = sbk_gem_yal
        elif require_gem:
            _require_executable(sbk_gem_yal, "SBK sbk-gem-yal")
        return SbkInstall(
            home=home,
            source=DependencySource.LOCAL,
            _sbk_yal=_require_executable(sbk_yal, "SBK sbk-yal"),
            _sbk_gem_yal=resolved_gem,
        )
    checked = ", ".join(str(home / "bin" / "sbk-yal") for home in homes)
    raise RuntimeError(
        "SBK local folder is not a ready-to-run distribution or built "
        f"checkout: {root}; checked: {checked}"
    )


def resolve_local_sbk_charts(folder: Path) -> ChartsInstall:
    """Resolve a ready-to-run local sbk-charts checkout or environment."""
    root = _local_directory(folder, "sbk-charts")
    if os.name == "nt":
        candidates = (
            root / "sbk-charts.exe",
            root / "Scripts" / "sbk-charts.exe",
        )
    else:
        candidates = (root / "sbk-charts", root / "bin" / "sbk-charts")
    for cli in candidates:
        if cli.is_file():
            return ChartsInstall(
                venv_dir=root,
                source=DependencySource.LOCAL,
                _cli=_require_executable(cli, "sbk-charts"),
                _python=Path(sys.executable),
            )
    checked = ", ".join(str(candidate) for candidate in candidates)
    raise RuntimeError(
        f"sbk-charts local folder has no supported executable: {root}; "
        f"checked: {checked}"
    )


def _gh_release(repo: str, tag: str, ssl_verify: bool = True) -> dict:
    """Fetch release metadata from GitHub for a given tag."""
    url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
    headers = {"Accept": "application/vnd.github+json"}
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
    
    log.info("fetching GitHub release metadata: %s@%s", repo, tag)
    r = requests.get(url, headers=headers, timeout=30, verify=ssl_verify)
    if r.status_code == 404:
        raise RuntimeError(f"GitHub release not found: {repo}@{tag}")
    r.raise_for_status()
    return r.json()


def _download(url: str, dest: Path, *, max_attempts: int = 6, ssl_verify: bool = True) -> None:
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
            with requests.get(url, stream=True, timeout=120, headers=headers, verify=ssl_verify) as r:
                if offset and r.status_code == 200:
                    # server ignored Range; restart from scratch
                    tmp.unlink(missing_ok=True)
                    offset = 0
                elif r.status_code not in (200, 206):
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
                    
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            # Show progress every 2 seconds
                            current_time = time.time()
                            if current_time - last_progress_time >= 2.0:
                                if total_size > 0:
                                    percent = (downloaded / total_size) * 100
                                    mb_downloaded = downloaded / (1024 * 1024)
                                    mb_total = total_size / (1024 * 1024)
                                    speed = (downloaded - last_progress_size) / (current_time - last_progress_time) / (1024 * 1024)
                                    progress_msg = f"  Download progress: {percent:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB, {speed:.1f} MB/s)"
                                    log.info(progress_msg)
                                    print(progress_msg, flush=True)
                                else:
                                    mb_downloaded = downloaded / (1024 * 1024)
                                    progress_msg = f"  Downloaded: {mb_downloaded:.1f} MB"
                                    log.info(progress_msg)
                                    print(progress_msg, flush=True)
                                
                                last_progress_time = current_time
                                last_progress_size = downloaded
                
                # Final progress report
                if total_size > 0:
                    percent = (downloaded / total_size) * 100
                    mb_downloaded = downloaded / (1024 * 1024)
                    mb_total = total_size / (1024 * 1024)
                    progress_msg = f"  Download complete: {percent:.1f}% ({mb_downloaded:.1f} MB / {mb_total:.1f} MB)"
                    log.info(progress_msg)
                    print(progress_msg, flush=True)
                else:
                    mb_downloaded = downloaded / (1024 * 1024)
                    progress_msg = f"  Download complete: {mb_downloaded:.1f} MB"
                    log.info(progress_msg)
                    print(progress_msg, flush=True)
                    
            tmp.replace(dest)
            return
        except (requests.exceptions.SSLError,
                requests.exceptions.ChunkedEncodingError,
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


def ensure_sbk(
    version: str,
    repo: str = "kmgowda/SBK",
    downloads_folder: Path | None = None,
    ssl_verify: bool = True,
    local_folder: Path | None = None,
    require_gem: bool = False,
) -> SbkInstall:
    """Resolve local SBK first, otherwise use/download the pinned release."""
    if local_folder is not None:
        log.info("using explicitly configured local SBK folder: %s", local_folder)
        return resolve_local_sbk(local_folder, require_gem=require_gem)

    # Use specified folder if provided, otherwise use cache
    if downloads_folder is None:
        cache = _cache_root() / "sbk" / version
    else:
        cache = downloads_folder / version
        cache.mkdir(parents=True, exist_ok=True)
    
    marker = cache / ".ok"
    home_file = cache / ".home"

    if marker.exists() and home_file.exists():
        home = Path(home_file.read_text().strip())
        # Validate that the extracted distribution still has the binaries.
        has_yal = (home / "bin" / "sbk-yal").exists()
        has_required_gem = (
            not require_gem or (home / "bin" / "sbk-gem-yal").exists()
        )
        if has_yal and has_required_gem:
            log.info("SBK %s already installed at %s (cache hit)", version, home)
            return SbkInstall(
                home=home, source=DependencySource.MANAGED_CACHE
            )
        log.warning(
            "SBK %s cache marker exists but binaries missing at %s; re-installing",
            version, home,
        )
        marker.unlink(missing_ok=True)

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

    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / Path(urlparse(url).path).name
    if not archive.exists():
        _download(url, archive, ssl_verify=ssl_verify)

    log.info("extracting SBK archive: %s", archive)
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

    _require_executable(home / "bin" / "sbk-yal", "downloaded SBK sbk-yal")
    if require_gem:
        _require_executable(
            home / "bin" / "sbk-gem-yal",
            "downloaded SBK sbk-gem-yal",
        )

    home_file.write_text(str(home.resolve()))
    marker.touch()
    # Free disk: the ~1+ GB archive is no longer needed once extracted.
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    log.info("SBK %s ready at %s", version, home)
    return SbkInstall(home=home, source=DependencySource.DOWNLOADED)


# ---------- sbk-charts ----------


def ensure_sbk_charts(
    version: str,
    repo_url: str = "https://github.com/kmgowda/sbk-charts",
    downloads_folder: Path | None = None,
    ssl_verify: bool = True,
    local_folder: Path | None = None,
) -> ChartsInstall:
    """Resolve local sbk-charts first, otherwise use conda/cache/download."""
    # Local selection must precede conda detection so an explicit path is
    # always authoritative and never silently replaced by another package.
    if local_folder is not None:
        log.info(
            "using explicitly configured local sbk-charts folder: %s",
            local_folder,
        )
        return resolve_local_sbk_charts(local_folder)

    # Check if we're in a conda environment - if so, install directly
    if "CONDA_PREFIX" in os.environ:
        log.info("Detected conda environment, installing sbk-charts directly")
        
        # Check if sbk-charts is already installed
        already_installed = False
        try:
            import importlib.metadata
            try:
                importlib.metadata.version("sbk-charts")
                already_installed = True
                log.info("sbk-charts already installed in conda environment")
            except importlib.metadata.PackageNotFoundError:
                pass
        except ImportError:
            # Python < 3.8, use pkg_resources
            try:
                import pkg_resources
                pkg_resources.get_distribution("sbk-charts")
                already_installed = True
                log.info("sbk-charts already installed in conda environment")
            except pkg_resources.DistributionNotFound:
                pass
        
        if already_installed:
            return ChartsInstall(
                venv_dir=Path(sys.prefix), source=DependencySource.CONDA
            )
        
        # Install sbk-charts in the current conda environment
        pip_url = repo_url.rstrip("/")
        if not pip_url.endswith(".git"):
            pip_url = pip_url + ".git"
        spec = f"git+{pip_url}@{version}"
        
        # Build pip command with optional SSL verification control
        pip_env = os.environ.copy()
        pip_args = [
            sys.executable,
            "-m",
            "pip",
            "install",
        ]
        
        if not ssl_verify:
            pip_args.extend([
                "--trusted-host", "github.com",
                "--trusted-host", "pypi.org",
                "--trusted-host", "files.pythonhosted.org",
                "--trusted-host", "pypi.python.org",
                "--trusted-host", "github.com",
                "--trusted-host", "raw.githubusercontent.com",
            ])
            # Also set environment variables for git
            pip_env["GIT_SSL_NO_VERIFY"] = "1"
            log.warning("SSL verification DISABLED for pip (ssl.verify=false in sbk-config.env)")
        else:
            log.debug("SSL verification enabled for pip (ssl.verify=true in sbk-config.env)")
        
        # Install sbk-charts
        cmd = pip_args + [spec]
        log.info("installing sbk-charts in conda environment: %s", spec)
        subprocess.run(cmd, check=True, env=pip_env)
        
        # Return a ChartsInstall pointing to the conda environment
        return ChartsInstall(
            venv_dir=Path(sys.prefix), source=DependencySource.CONDA
        )
    
    # Use downloads_folder for caching if provided, otherwise use cache
    if downloads_folder is None:
        cache = _cache_root() / "sbk-charts" / version
    else:
        cache = downloads_folder / "sbk-charts" / version
        cache.mkdir(parents=True, exist_ok=True)
    
    venv_dir = cache / "venv"
    marker = cache / ".ok"

    install = ChartsInstall(
        venv_dir=venv_dir, source=DependencySource.MANAGED_CACHE
    )
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

    log.info("creating venv for sbk-charts %s at %s", version, venv_dir)
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(venv_dir)

    # Install from the GitHub tag (release source tarball)
    # pip wants 'git+<url>.git@<ref>' for VCS installs
    pip_url = repo_url.rstrip("/")
    if not pip_url.endswith(".git"):
        pip_url = pip_url + ".git"
    spec = f"git+{pip_url}@{version}"

    # Build pip command with optional SSL verification control
    pip_env = os.environ.copy()
    pip_args = [
        str(install.python),
        "-m",
        "pip",
        "install",
    ]
    
    if not ssl_verify:
        pip_args.extend([
            "--trusted-host", "github.com",
            "--trusted-host", "pypi.org",
            "--trusted-host", "files.pythonhosted.org",
            "--trusted-host", "pypi.python.org",
            "--trusted-host", "github.com",
            "--trusted-host", "raw.githubusercontent.com",
        ])
        # Also set environment variables for git
        pip_env["GIT_SSL_NO_VERIFY"] = "1"
        log.warning("SSL verification DISABLED for pip (ssl.verify=false in sbk-config.env)")
    else:
        log.debug("SSL verification enabled for pip (ssl.verify=true in sbk-config.env)")
    
    # Upgrade pip first
    cmd = pip_args + ["--quiet", "--upgrade", "pip"]
    log.info("upgrading pip in venv")
    subprocess.run(cmd, check=True, env=pip_env)
    
    # Install sbk-charts
    cmd = pip_args + [spec]
    log.info("installing sbk-charts: %s", spec)
    subprocess.run(cmd, check=True, env=pip_env)

    if not install.cli.exists():
        # some versions expose differently named entry points
        bindir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
        candidates = list(bindir.glob("sbk-charts*")) + list(bindir.glob("sb-charts*"))
        if candidates:
            install = ChartsInstall(
                venv_dir=venv_dir,
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

    marker.touch()
    log.info("sbk-charts %s installed successfully", version)
    return ChartsInstall(
        venv_dir=venv_dir,
        source=DependencySource.DOWNLOADED,
        _cli=install.cli,
        _python=install.python,
    )


# ---------- JDK (Adoptium / Temurin) ----------

DEFAULT_JDK_VERSION = "25"
ADOPTIUM_BINARY_URL = (
    "https://api.adoptium.net/v3/binary/latest/{version}/ga/"
    "{os}/{arch}/jdk/hotspot/normal/eclipse"
)


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
            capture_output=True, text=True, timeout=10,
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
    v = os.environ.get("SBK_JAVA_HOME")
    if v:
        _push(Path(v))

    # 2. JAVA_HOME - second priority
    v = os.environ.get("JAVA_HOME")
    if v:
        _push(Path(v))

    # 3. java on PATH -> derive home as the parent of <home>/bin/java.
    from shutil import which
    java = which("java")
    if java:
        jp = Path(java)
        if jp.parent.name == "bin":
            _push(jp.parent.parent)

    return candidates


def find_existing_jdk(required_major: int) -> Path | None:
    """Return the home of an already-installed JDK whose major version
    matches ``required_major``, or ``None`` if no match is found.

    Probes (in order): ``SBK_JAVA_HOME`` env var, ``JAVA_HOME`` env var,
    then ``java`` on ``PATH``.
    """
    for home in _candidate_jdk_homes():
        java = home / "bin" / "java"
        if os.name == "nt":
            java = home / "bin" / "java.exe"
        if not java.exists():
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


def _jdk_url(version: str) -> str:
    arch = "x64" if os.uname().machine in ("x86_64", "amd64") else os.uname().machine
    os_name = {
        "linux": "linux",
        "darwin": "mac",
        "win32": "windows",
    }.get(sys.platform, sys.platform)
    return ADOPTIUM_BINARY_URL.format(version=version, os=os_name, arch=arch)


def ensure_jdk(version: str = DEFAULT_JDK_VERSION, jdk_folder: Path | None = None, ssl_verify: bool = True) -> JdkInstall:
    """Ensure a JDK of the given major version is available.

    Resolution order (exactly as specified):

    1. **SBK_JAVA_HOME** -- if set and points to the required version, use it.
    2. **JAVA_HOME** -- if set and points to the required version, use it.
    3. **java on PATH** -- if it reports the required version, use it.
    4. **Specified folder** -- if jdk_folder is provided and contains the required version, use it.
    5. **Download** -- fetch Temurin of the requested major version from
       the Adoptium API, extract it under the specified folder (or cache if not specified),
       and set SBK_JAVA_HOME to point to it for current and future builds.
    """
    try:
        required_major = int(version)
    except ValueError:
        log.error("Invalid JDK version '%s', must be a number", version)
        raise ValueError(f"Invalid JDK version: {version}")

    # Use specified folder if provided, otherwise use cache
    if jdk_folder is None:
        cache = _cache_root() / "jdk" / version
    else:
        cache = jdk_folder / version
        cache.mkdir(parents=True, exist_ok=True)
    
    marker = cache / ".ok"
    home_file = cache / ".home"

    # Helper function to check if a Java installation matches the required version
    def _check_java_home(java_home: Path) -> bool:
        """Check if java_home contains a JDK matching the required version."""
        if not java_home:
            return False
        java_path = java_home / "bin" / "java"
        if os.name == "nt":
            java_path = java_home / "bin" / "java.exe"
        if not java_path.exists():
            log.debug("Java home %s does not contain bin/java", java_home)
            return False
        major = _java_major_version(java_path)
        log.debug("Java home %s reports major=%s (required=%s)", java_home, major, required_major)
        return major == required_major

    # 1. Check SBK_JAVA_HOME first (highest priority)
    sbk_java_home = os.environ.get("SBK_JAVA_HOME")
    if sbk_java_home:
        log.info("Checking SBK_JAVA_HOME=%s", sbk_java_home)
        if _check_java_home(Path(sbk_java_home)):
            log.info("SBK_JAVA_HOME points to JDK %s at %s; using it", required_major, sbk_java_home)
            return JdkInstall(home=Path(sbk_java_home))
        else:
            log.warning("SBK_JAVA_HOME is set but does not contain JDK %s", required_major)

    # 2. Check JAVA_HOME (second priority)
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        log.info("Checking JAVA_HOME=%s", java_home)
        if _check_java_home(Path(java_home)):
            log.info("JAVA_HOME points to JDK %s at %s; using it", required_major, java_home)
            # Set SBK_JAVA_HOME to point to this JDK
            os.environ["SBK_JAVA_HOME"] = str(Path(java_home).resolve())
            return JdkInstall(home=Path(java_home))
        else:
            log.warning("JAVA_HOME is set but does not contain JDK %s", required_major)

    # 3. Check java on PATH (third priority)
    from shutil import which
    java_exe = which("java")
    if java_exe:
        log.info("Checking java on PATH: %s", java_exe)
        java_path = Path(java_exe)
        # Derive JDK home from java executable location
        if java_path.parent.name == "bin":
            jdk_home = java_path.parent.parent
            if _check_java_home(jdk_home):
                log.info("java on PATH points to JDK %s at %s; using it", required_major, jdk_home)
                # Set SBK_JAVA_HOME to point to this JDK
                os.environ["SBK_JAVA_HOME"] = str(jdk_home.resolve())
                return JdkInstall(home=jdk_home)
        else:
            log.debug("java on PATH is not in a JDK bin directory")
        # Also check the version directly
        major = _java_major_version(java_path)
        if major == required_major:
            log.info("java on PATH reports JDK %s; using it (though JDK home location is unclear)", required_major)
            # We can't set SBK_JAVA_HOME properly in this case, but we can return the java path
            # However, this might not work for SBK which needs the full JDK home
            log.warning("java on PATH matches version %s but JDK home location unclear; may not work for SBK", required_major)

    # 4. Check specified folder for cached version (fourth priority)
    if jdk_folder and marker.exists() and home_file.exists():
        cached_home = Path(home_file.read_text().strip())
        log.info("Checking cached JDK in specified folder: %s", cached_home)
        if _check_java_home(cached_home):
            log.info("JDK %s found in specified folder %s (cache hit)", required_major, cached_home)
            # Set SBK_JAVA_HOME to point to this JDK
            os.environ["SBK_JAVA_HOME"] = str(cached_home.resolve())
            return JdkInstall(home=cached_home)
        else:
            log.warning("Cached JDK in specified folder does not match version %s; re-downloading", required_major)
            marker.unlink(missing_ok=True)

    # 5. Download JDK to specified folder or cache (fifth priority)
    log.info("No JDK %s found in SBK_JAVA_HOME, JAVA_HOME, PATH, or cache; downloading Temurin JDK %s to %s", required_major, version, cache)
    cache.mkdir(parents=True, exist_ok=True)

    url = _jdk_url(version)
    archive = cache / f"jdk-{version}.tar.gz"
    if not archive.exists():
        _download(url, archive, ssl_verify=ssl_verify)

    extract_dir = cache / "extracted"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    top = _extract(archive, extract_dir)

    # locate the actual JDK home (folder containing bin/java)
    home = top
    if not (home / "bin" / "java").exists():
        for sub in home.rglob("bin"):
            if (sub / "java").exists():
                home = sub.parent
                break

    if not (home / "bin" / "java").exists():
        raise RuntimeError(
            f"extracted JDK does not contain bin/java under {extract_dir}"
        )

    home_file.write_text(str(home.resolve()))
    marker.touch()
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    
    # Set SBK_JAVA_HOME to point to this JDK (not JAVA_HOME)
    os.environ["SBK_JAVA_HOME"] = str(home.resolve())
    log.info("JDK %s downloaded and ready at %s (SBK_JAVA_HOME set for current and future builds)", version, home)
    return JdkInstall(home=home)
