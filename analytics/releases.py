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

import hashlib
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
from contextlib import contextmanager
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

import requests

from .errors import CacheError, DependencyResolutionError, LocalPackageError

log = logging.getLogger(__name__)


def cache_root() -> Path:
    """Return the environment-selected cache, then the platform default."""
    root = os.environ.get("SBK_ANALYTICS_DOWNLOADS_FOLDER") or os.environ.get(
        "SBK_ANALYTICS_CACHE"
    )
    if root:
        return Path(root)
    return Path.home() / ".cache" / "sbk-analytics"


def _cache_root() -> Path:
    """Backward-compatible private alias used by older callers/tests."""
    return cache_root()


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
    detected_version: str | None = None

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
    detected_version: str | None = None

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
        raise LocalPackageError(
            f"{dependency} local folder does not exist: {folder}"
        ) from exc
    if not root.is_dir():
        raise LocalPackageError(
            f"{dependency} local folder is not a directory: {root}"
        )
    return root


def _require_executable(path: Path, dependency: str) -> Path:
    """Validate a local command without modifying its permissions."""
    if not path.is_file():
        raise LocalPackageError(f"{dependency} executable is missing: {path}")
    if not os.access(path, os.X_OK):
        raise LocalPackageError(f"{dependency} executable is not executable: {path}")
    return path


def _command_version(command: Path, args: list[str], pattern: str) -> str | None:
    try:
        result = subprocess.run(
            [str(command), *args], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    stderr = result.stderr if isinstance(result.stderr, str) else ""
    match = re.search(pattern, stdout + stderr, re.I)
    return match.group(1) if match else None


def _charts_version(cli: Path, *, require_ready: bool = False) -> str | None:
    try:
        result = subprocess.run(
            [str(cli), "-h"], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired as exc:
        if require_ready:
            raise LocalPackageError(
                f"sbk-charts readiness check timed out after 60s: {cli}"
            ) from exc
        result = None
    if result is not None:
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        stderr = result.stderr if isinstance(result.stderr, str) else ""
        output = stdout + stderr
        match = re.search(
            r"(?:Sbk Charts Version\s*:\s*)?(\d+(?:\.\d+)+)",
            output,
            re.I,
        )
        if require_ready and result.returncode != 0:
            raise LocalPackageError(
                f"sbk-charts readiness check failed (rc={result.returncode}): "
                f"{cli}; {output.strip()[-500:]}"
            )
        if match:
            return match.group(1)
    python = cli.parent / ("python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        return None
    return _command_version(
        python,
        ["-c", "import importlib.metadata as m; print(m.version('sbk-charts'))"],
        r"(\d+(?:\.\d+)+)",
    )


def _check_version(name: str, detected: str | None, expected: str, policy: str) -> None:
    if policy == "ignore":
        return
    if detected == expected:
        return
    message = (
        f"{name} version mismatch: configured {expected!r}, "
        f"detected {detected or 'unknown'!r}"
    )
    if policy == "exact":
        raise LocalPackageError(message)
    log.warning("%s (policy=warn)", message)


def resolve_local_sbk(
    folder: Path, *, require_gem: bool = False, expected_version: str = "",
    version_policy: str = "warn",
) -> SbkInstall:
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
        detected = _command_version(
            sbk_yal, ["-help"], r"SBK(?:-YAL)?\s+Version:\s*([^\s]+)"
        )
        if expected_version:
            _check_version("SBK", detected, expected_version, version_policy)
        return SbkInstall(
            home=home,
            source=DependencySource.LOCAL,
            _sbk_yal=_require_executable(sbk_yal, "SBK sbk-yal"),
            _sbk_gem_yal=resolved_gem,
            detected_version=detected,
        )
    checked = ", ".join(str(home / "bin" / "sbk-yal") for home in homes)
    raise LocalPackageError(
        "SBK local folder is not a ready-to-run distribution or built "
        f"checkout: {root}; checked: {checked}"
    )


def resolve_local_sbk_charts(
    folder: Path | None = None, *, executable: Path | None = None,
    expected_version: str = "", version_policy: str = "warn",
    preflight: bool = False,
) -> ChartsInstall:
    """Resolve a ready-to-run local sbk-charts checkout or environment."""
    if executable is not None:
        cli = executable.expanduser().resolve(strict=True)
        _require_executable(cli, "sbk-charts")
        root = cli.parent
        candidates = (cli,)
    elif folder is not None:
        root = _local_directory(folder, "sbk-charts")
        if os.name == "nt":
            candidates = (root / "sbk-charts.exe", root / "Scripts" / "sbk-charts.exe")
        else:
            candidates = (root / "sbk-charts", root / "bin" / "sbk-charts")
    else:
        raise LocalPackageError("sbk-charts local folder or executable is required")
    for cli in candidates:
        if cli.is_file():
            detected = _charts_version(cli, require_ready=preflight)
            if expected_version:
                _check_version("sbk-charts", detected, expected_version, version_policy)
            return ChartsInstall(
                venv_dir=root,
                source=DependencySource.LOCAL,
                _cli=_require_executable(cli, "sbk-charts"),
                _python=Path(sys.executable),
                detected_version=detected,
            )
    checked = ", ".join(str(candidate) for candidate in candidates)
    raise LocalPackageError(
        f"sbk-charts local folder has no supported executable: {root}; "
        f"checked: {checked}"
    )


def _gh_release(repo: str, tag: str, ssl_verify: bool | str = False) -> dict:
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


def _download(
    url: str, dest: Path, *, max_attempts: int = 6,
    ssl_verify: bool | str = False,
) -> str:
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
            digest = hashlib.sha256()
            with dest.open("rb") as downloaded_file:
                for block in iter(lambda: downloaded_file.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()
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
    raise DependencyResolutionError(
        f"failed to download {url} after {max_attempts} attempts"
    ) from last_err


@contextmanager
def _cache_lock(lock_path: Path):
    """Serialize installers that target the same dependency/version."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        if os.name == "nt":
            import msvcrt
            if lock_path.stat().st_size == 0:
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _write_metadata(path: Path, **values) -> None:
    values["installed_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")


def _extract(archive: Path, dest: Path) -> Path:
    """Extract archive into dest/, return the (single) top-level dir inside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    destination = dest.resolve()

    def safe(member_name: str) -> None:
        candidate = (destination / member_name).resolve()
        if candidate != destination and destination not in candidate.parents:
            raise CacheError(f"unsafe archive path rejected: {member_name}")

    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                safe(member.filename)
                mode = member.external_attr >> 16
                if (mode & 0o170000) == 0o120000:
                    raise CacheError(f"archive symlink rejected: {member.filename}")
            zf.extractall(dest)
    elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2")):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                safe(member.name)
                if member.issym() or member.islnk():
                    raise CacheError(f"archive link rejected: {member.name}")
                if member.isdev() or member.isfifo():
                    raise CacheError(
                        f"archive special file rejected: {member.name}"
                    )
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
    ssl_verify: bool | str = False,
    local_folder: Path | None = None,
    require_gem: bool = False,
    version_policy: str = "warn",
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
        cache = _cache_root() / "sbk" / version
    else:
        cache = downloads_folder / version
        cache.mkdir(parents=True, exist_ok=True)
    
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache.parent / f".{cache.name}.lock"):
        return _ensure_sbk_locked(
            version, repo, cache, ssl_verify, require_gem
        )


def _ensure_sbk_locked(
    version: str, repo: str, cache: Path, ssl_verify: bool | str,
    require_gem: bool,
) -> SbkInstall:
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

    stage = cache.with_name(f".{cache.name}.install-{os.getpid()}")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

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

    archive = stage / Path(urlparse(url).path).name
    if not archive.exists():
        checksum = _download(url, archive, ssl_verify=ssl_verify)
    else:
        checksum = None
    expected_digest = asset.get("digest")
    if expected_digest and expected_digest.startswith("sha256:"):
        expected_sha256 = expected_digest.split(":", 1)[1].lower()
        if checksum != expected_sha256:
            raise CacheError(
                f"SBK asset checksum mismatch for {asset['name']}: "
                f"expected {expected_sha256}, got {checksum}"
            )

    log.info("extracting SBK archive: %s", archive)
    extract_dir = stage / "extracted"
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

    relative_home = home.relative_to(stage)
    final_home = cache / relative_home
    (stage / ".home").write_text(str(final_home.resolve()))
    _write_metadata(
        stage / "metadata.json", dependency="sbk", version=version,
        source_url=url, asset=asset["name"], sha256=checksum,
        executables={"sbk-yal": str(final_home / "bin" / "sbk-yal"),
                     "sbk-gem-yal": str(final_home / "bin" / "sbk-gem-yal")},
    )
    # Free disk: the ~1+ GB archive is no longer needed once extracted.
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    # Completion is written inside the staging directory and the entire
    # installation is then atomically published under the version path.
    (stage / ".ok").touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    log.info("SBK %s ready at %s", version, final_home)
    return SbkInstall(home=final_home, source=DependencySource.DOWNLOADED)


# ---------- sbk-charts ----------


def ensure_sbk_charts(
    version: str,
    repo_url: str = "https://github.com/kmgowda/sbk-charts",
    downloads_folder: Path | None = None,
    ssl_verify: bool | str = False,
    local_folder: Path | None = None,
    local_executable: Path | None = None,
    version_policy: str = "warn",
    preflight: bool = False,
) -> ChartsInstall:
    """Resolve local sbk-charts first, otherwise use conda/cache/download."""
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

    # Check if we're in a conda environment - if so, install directly
    if "CONDA_PREFIX" in os.environ:
        log.info("Detected conda environment, installing sbk-charts directly")
        
        # Check if sbk-charts is already installed
        already_installed = False
        installed_version = None
        try:
            import importlib.metadata
            try:
                installed_version = importlib.metadata.version("sbk-charts")
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
        
        if already_installed and installed_version != version:
            message = (
                f"conda sbk-charts version mismatch: configured {version!r}, "
                f"installed {installed_version!r}"
            )
            if version_policy == "exact":
                raise DependencyResolutionError(message)
            if version_policy == "warn":
                log.warning(message)
        if already_installed:
            return ChartsInstall(
                venv_dir=Path(sys.prefix), source=DependencySource.CONDA,
                detected_version=installed_version,
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
        elif isinstance(ssl_verify, str):
            pip_env["PIP_CERT"] = ssl_verify
            pip_env["GIT_SSL_CAINFO"] = ssl_verify
            log.info("using custom CA bundle for pip/git: %s", ssl_verify)
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
    
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache.parent / f".{cache.name}.lock"):
        return _ensure_sbk_charts_locked(
            version, repo_url, cache, ssl_verify
        )


def _ensure_sbk_charts_locked(
    version: str, repo_url: str, cache: Path, ssl_verify: bool | str,
) -> ChartsInstall:
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

    stage = cache.with_name(f".{cache.name}.install-{os.getpid()}")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    stage_venv = stage / "venv"
    install = ChartsInstall(
        venv_dir=stage_venv, source=DependencySource.DOWNLOADED
    )
    log.info("creating venv for sbk-charts %s at %s", version, stage_venv)
    builder = venv.EnvBuilder(with_pip=True, clear=True)
    builder.create(stage_venv)

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
    elif isinstance(ssl_verify, str):
        pip_env["PIP_CERT"] = ssl_verify
        pip_env["GIT_SSL_CAINFO"] = ssl_verify
        log.info("using custom CA bundle for pip/git: %s", ssl_verify)
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
        bindir = stage_venv / ("Scripts" if os.name == "nt" else "bin")
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

    relative_cli = install.cli.relative_to(stage)
    relative_python = install.python.relative_to(stage)
    final_cli = cache / relative_cli
    final_python = cache / relative_python
    _write_metadata(
        stage / "metadata.json", dependency="sbk-charts", version=version,
        source_url=repo_url, executable=str(final_cli), spec=spec,
    )
    (stage / ".ok").touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    log.info("sbk-charts %s installed successfully", version)
    return ChartsInstall(
        venv_dir=cache / "venv",
        source=DependencySource.DOWNLOADED,
        _cli=final_cli,
        _python=final_python,
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


def ensure_jdk(
    version: str = DEFAULT_JDK_VERSION, jdk_folder: Path | None = None,
    ssl_verify: bool | str = False,
) -> JdkInstall:
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
    cache.parent.mkdir(parents=True, exist_ok=True)
    with _cache_lock(cache.parent / f".{cache.name}.lock"):
        # Another process may have completed the install while this process
        # waited for the lock.
        if marker.exists() and home_file.exists():
            cached_home = Path(home_file.read_text().strip())
            if _check_java_home(cached_home):
                os.environ["SBK_JAVA_HOME"] = str(cached_home.resolve())
                return JdkInstall(home=cached_home)
        return _install_jdk_locked(version, cache, ssl_verify)


def _install_jdk_locked(
    version: str, cache: Path, ssl_verify: bool | str,
) -> JdkInstall:
    """Download, validate, and atomically publish one managed JDK."""
    stage = cache.with_name(f".{cache.name}.install-{os.getpid()}")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    url = _jdk_url(version)
    archive = stage / f"jdk-{version}.tar.gz"
    checksum = _download(url, archive, ssl_verify=ssl_verify)

    extract_dir = stage / "extracted"
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

    relative_home = home.relative_to(stage)
    final_home = cache / relative_home
    (stage / ".home").write_text(str(final_home.resolve()))
    _write_metadata(
        stage / "metadata.json", dependency="jdk", version=version,
        source_url=url, sha256=checksum,
        executable=str(final_home / "bin" / "java"),
    )
    try:
        archive.unlink()
    except OSError as e:
        log.debug("could not remove archive %s: %s", archive, e)
    
    # Set SBK_JAVA_HOME to point to this JDK (not JAVA_HOME)
    (stage / ".ok").touch()
    if cache.exists():
        shutil.rmtree(cache)
    stage.replace(cache)
    os.environ["SBK_JAVA_HOME"] = str(final_home.resolve())
    log.info("JDK %s downloaded and ready at %s (SBK_JAVA_HOME set)", version, final_home)
    return JdkInstall(home=final_home)
