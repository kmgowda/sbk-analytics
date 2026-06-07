"""Execute sbk-yal / sbk-gem-yal instances in serial or parallel mode."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PARALLEL_WARNING = (
    "WARNING: parallel mode is experimental. Multiple SBK instances will run "
    "concurrently against potentially shared storage backends, which may distort "
    "benchmark results. Per-instance stdout/stderr is captured to log files; only "
    "a progress heartbeat is printed every 5 seconds."
)


@dataclass
class RunResult:
    class_name: str
    yml_path: Path
    csv_path: Path
    log_path: Path | None
    returncode: int
    duration_s: float

    @property
    def ok(self) -> bool:
        """A run is treated as successful when a non-empty CSV exists.

        We deliberately do NOT require ``returncode == 0`` because SBK 9.0's
        JVM can hang on shutdown for some drivers (notably RocksDB) after the
        benchmark itself has completed and the CSV has been flushed. In that
        case the watchdog SIGKILLs the JVM and the returncode is negative,
        but the CSV is complete and usable.
        """
        return self.csv_path.exists() and self.csv_path.stat().st_size > 0


def _build_cmd(executable: Path, yml_path: Path) -> list[str]:
    return [str(executable), "-f", str(yml_path)]


def _expected_seconds(yml_path: Path) -> int | None:
    """Read the benchmark's `seconds:` setting out of an SBK YAL/GEM-YAL YAML.
    Returns None if not set (the run is open-ended and shouldn't be timed out).
    """
    try:
        data = yaml.safe_load(yml_path.read_text()) or {}
    except OSError:
        return None
    if not isinstance(data, dict):
        return None
    for wrapper in ("sbkArgs", "sbkGemArgs"):
        block = data.get(wrapper)
        if isinstance(block, dict) and "seconds" in block:
            try:
                return int(block["seconds"])
            except (TypeError, ValueError):
                return None
    return None


DEFAULT_KILL_GRACE_S = 5.0


def _hung_jvm_watchdog(
    proc: subprocess.Popen,
    csv_path: Path,
    *,
    expected_seconds: int | None,
    kill_grace_s: float = DEFAULT_KILL_GRACE_S,
    poll_interval_s: float = 0.5,
) -> int:
    """Wait for ``proc`` to exit, force-killing it ``kill_grace_s`` seconds
    after the configured benchmark duration.

    SBK 9.0 sometimes fails to release native threads (notably for the
    RocksDB driver) after the benchmark has flushed its CSV. Per spec, if the
    SBK instance has not closed/completed within ``seconds + 5`` (configurable
    via ``kill_grace_s``), the process is killed forcefully and the CSV file
    that exists on disk at that moment is used by sbk-charts.

    If the YAML did not set ``seconds`` (open-ended benchmark) no timeout
    applies and we simply wait for the process to exit.
    """
    start = time.monotonic()
    deadline = (
        start + expected_seconds + kill_grace_s
        if expected_seconds is not None
        else None
    )
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            try:
                size = csv_path.stat().st_size if csv_path.exists() else 0
            except OSError:
                size = 0
            log.warning(
                "SBK instance did not complete within %ds + %ds grace; "
                "killing (csv=%d bytes)",
                expected_seconds, int(kill_grace_s), size,
            )
            proc.terminate()
            try:
                return proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                return proc.wait()
        time.sleep(poll_interval_s)


def _run_serial(
    executable: Path,
    jobs: list[tuple[str, Path, Path]],  # (instance_name, yml, csv)
) -> list[RunResult]:
    results: list[RunResult] = []
    for class_name, yml_path, csv_path in jobs:
        seconds = _expected_seconds(yml_path)
        log.info(
            "[serial] starting class=%s yml=%s expected_seconds=%s",
            class_name, yml_path, seconds,
        )
        start = time.monotonic()
        # stdout/stderr inherited so the user sees output live
        proc = subprocess.Popen(_build_cmd(executable, yml_path))
        rc = _hung_jvm_watchdog(proc, csv_path, expected_seconds=seconds)
        dur = time.monotonic() - start
        results.append(
            RunResult(
                class_name=class_name,
                yml_path=yml_path,
                csv_path=csv_path,
                log_path=None,
                returncode=rc,
                duration_s=dur,
            )
        )
        log.info("[serial] finished class=%s rc=%s (%.1fs)", class_name, rc, dur)
    return results


def _run_parallel(
    executable: Path,
    jobs: list[tuple[str, Path, Path]],
    log_dir: Path,
) -> list[RunResult]:
    print(PARALLEL_WARNING, file=sys.stderr, flush=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    procs: list[
        tuple[str, Path, Path, Path, subprocess.Popen, float, float | None]
    ] = []
    for class_name, yml_path, csv_path in jobs:
        log_path = log_dir / f"sbk-{class_name}.log"
        f = log_path.open("w")
        seconds = _expected_seconds(yml_path)
        log.info(
            "[parallel] launching class=%s log=%s expected_seconds=%s",
            class_name, log_path, seconds,
        )
        start = time.monotonic()
        p = subprocess.Popen(
            _build_cmd(executable, yml_path),
            stdout=f,
            stderr=subprocess.STDOUT,
        )
        deadline = (start + seconds + DEFAULT_KILL_GRACE_S) if seconds else None
        procs.append((class_name, yml_path, csv_path, log_path, p, start, deadline))

    # heartbeat loop
    pending = {i for i in range(len(procs))}
    last_print = 0.0
    HEARTBEAT = 5.0
    while pending:
        time.sleep(0.5)
        now = time.monotonic()
        for i in list(pending):
            class_name, _, csv_path, _, p, _, deadline = procs[i]
            if p.poll() is not None:
                pending.discard(i)
                continue
            if deadline is not None and now >= deadline:
                size = csv_path.stat().st_size if csv_path.exists() else 0
                log.warning(
                    "[parallel] class=%s did not complete within %ds + %ds grace; "
                    "killing (csv=%d bytes)",
                    class_name,
                    int(deadline - procs[i][5] - DEFAULT_KILL_GRACE_S),
                    int(DEFAULT_KILL_GRACE_S),
                    size,
                )
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                pending.discard(i)
        if pending and (now - last_print) >= HEARTBEAT:
            running = [procs[i][0] for i in pending]
            elapsed = [f"{procs[i][0]}={now - procs[i][5]:.0f}s" for i in pending]
            print(
                f"[parallel] {len(running)} running: {', '.join(elapsed)}",
                flush=True,
            )
            last_print = now

    results: list[RunResult] = []
    for class_name, yml_path, csv_path, log_path, p, start, _ in procs:
        # ensure file handle flushed
        try:
            if p.stdout is not None:
                p.stdout.close()
        except Exception:
            pass
        dur = time.monotonic() - start
        results.append(
            RunResult(
                class_name=class_name,
                yml_path=yml_path,
                csv_path=csv_path,
                log_path=log_path,
                returncode=p.returncode if p.returncode is not None else -1,
                duration_s=dur,
            )
        )
    return results


def run_jobs(
    executable: Path,
    jobs: list[tuple[str, Path, Path]],
    *,
    mode: str,
    log_dir: Path,
) -> list[RunResult]:
    if not executable.exists():
        raise FileNotFoundError(f"SBK executable not found: {executable}")
    if mode == "parallel":
        return _run_parallel(executable, jobs, log_dir)
    return _run_serial(executable, jobs)
