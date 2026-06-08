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


def _read_yml(yml_path: Path) -> tuple[dict, bool]:
    """Return ``(params, is_gem)`` from an SBK YAL/GEM-YAL YAML.

    ``params`` is the dict inside the ``sbkArgs:`` or ``sbkGemArgs:`` wrapper.
    ``is_gem`` is True when the wrapper is ``sbkGemArgs:`` (i.e. sbk-gem-yal).
    """
    try:
        data = yaml.safe_load(yml_path.read_text()) or {}
    except OSError:
        return {}, False
    if not isinstance(data, dict):
        return {}, False
    if isinstance(data.get("sbkGemArgs"), dict):
        return data["sbkGemArgs"], True
    if isinstance(data.get("sbkArgs"), dict):
        return data["sbkArgs"], False
    return {}, False


def _expected_seconds(yml_path: Path) -> int | None:
    """Read the benchmark's ``seconds:`` setting out of an SBK YAL/GEM-YAL YAML.

    Returns the configured value when it is a positive integer. Returns ``None``
    in any of the following cases (which all mean *no timeout*):

    - ``seconds:`` is not set in the YAML
    - ``seconds:`` is set to a non-positive value (``0`` / negative)
    - the YAML cannot be parsed

    A return of ``None`` is the signal to the watchdog that the benchmark is
    bounded by records or runs forever, and that the SBK process should NOT be
    killed by sbk-analytics.
    """
    params, _ = _read_yml(yml_path)
    if "seconds" not in params:
        return None
    try:
        secs = int(params["seconds"])
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


# ---- Kill grace ----------------------------------------------------------
#
# Both sbk-yal and sbk-gem-yal use the same total ``seconds + 15`` window
# before sbk-analytics force-kills the local SBK process. For sbk-gem-yal we
# additionally fire an SSH ``pkill`` against every node 5 seconds earlier
# (``seconds + 10``) so the remote sbk clients are killed first; the local
# sbk-gem-yal then has 5 seconds to notice and shut down cleanly before we
# kill it too.

# When to kill remote sbk clients spawned by sbk-gem-yal (gem mode only).
REMOTE_KILL_GRACE_S = 10.0
# When to kill the local sbk-yal / sbk-gem-yal process (both modes).
LOCAL_KILL_GRACE_S = 15.0


# ---- Remote SBK kill (sbk-gem-yal only) ---------------------------------


def _remote_kill_pattern() -> str:
    """A pattern to match the Java SBK process on a remote node.

    Matches the SBK main classes used by both sbk(-yal) and sbk-gem-yal-spawned
    remote sbk clients. Restricted to Java processes whose command line
    mentions ``io.sbk.main.`` so we don't kill unrelated programs.
    """
    return "io.sbk.main"


def _kill_remote_sbk_clients(yml_path: Path) -> None:
    """Best-effort: SSH into every node listed in the gem-yal YAML and pkill
    the remote SBK clients spawned by sbk-gem-yal.

    Uses the same credentials sbk-gem-yal itself used: ``gemuser`` / ``gempass``
    / ``gemport`` (default 22). Requires ``sshpass`` on PATH when ``gempass``
    is supplied; without it we still try keys-only ssh. Failures are logged
    and do not propagate -- the local sbk-gem-yal process has already been
    killed by the caller, and the CSV is preserved.
    """
    params, is_gem = _read_yml(yml_path)
    if not is_gem:
        return
    nodes_raw = params.get("nodes")
    if not nodes_raw:
        return

    # nodes can be a comma- or whitespace-separated string, or a list
    if isinstance(nodes_raw, (list, tuple)):
        nodes = [str(n).strip() for n in nodes_raw if str(n).strip()]
    else:
        s = str(nodes_raw)
        for sep in (",", "\n", "\t"):
            s = s.replace(sep, " ")
        nodes = [n for n in s.split() if n]

    if not nodes:
        return

    user = str(params.get("gemuser", "")).strip()
    password = str(params.get("gempass", "")).strip()
    try:
        port = int(params.get("gemport", 22))
    except (TypeError, ValueError):
        port = 22

    have_sshpass = bool(password) and _which("sshpass") is not None
    if password and not have_sshpass:
        log.warning(
            "remote sbk kill: 'sshpass' not on PATH but gempass is set; "
            "attempting key-based ssh which may fail"
        )

    threads: list[threading.Thread] = []
    for node in nodes:
        t = threading.Thread(
            target=_ssh_pkill_one,
            args=(node, user, password, port, have_sshpass),
            daemon=True,
        )
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=15)


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


def _ssh_pkill_one(node: str, user: str, password: str, port: int,
                   have_sshpass: bool) -> None:
    remote_cmd = f"pkill -9 -f {_remote_kill_pattern()} || true"
    target = f"{user}@{node}" if user else node
    ssh_args = [
        "ssh",
        "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=" + ("no" if password else "yes"),
        "-o", "ConnectTimeout=5",
        target,
        remote_cmd,
    ]
    if have_sshpass and password:
        env = {**os.environ, "SSHPASS": password}
        cmd = ["sshpass", "-e", *ssh_args]
    else:
        env = os.environ.copy()
        cmd = ssh_args
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            log.info("remote sbk kill: %s OK", node)
        else:
            log.warning(
                "remote sbk kill: %s failed rc=%s: %s",
                node, proc.returncode, (proc.stderr or "").strip()[:200],
            )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("remote sbk kill: %s error: %s", node, e)


def _hung_jvm_watchdog(
    proc: subprocess.Popen,
    csv_path: Path,
    yml_path: Path,
    *,
    expected_seconds: int | None,
    is_gem: bool,
    poll_interval_s: float = 0.5,
) -> int:
    """Wait for ``proc`` to exit, force-killing it after the benchmark window.

    Timing (with ``seconds`` taken from the YAML):

    - At ``seconds + 10`` (gem mode only): SSH into every ``nodes:`` entry
      and ``pkill -9 -f io.sbk.main`` so the remote sbk clients are killed
      first. Done once, in a background thread (so it does not delay the
      local kill).
    - At ``seconds + 15`` (both modes): SIGTERM then SIGKILL the local
      ``sbk-yal`` / ``sbk-gem-yal`` process. For gem mode we wait briefly
      for the remote-kill thread to finish first so the killed-remote logs
      are sequenced correctly with the local kill log.

    If ``expected_seconds`` is ``None`` (benchmark bounded by ``records:`` or
    runs forever) no timeout applies and we just wait for the process to
    exit. sbk-analytics never kills the local SBK process or the remote sbk
    clients in that case.
    """
    start = time.monotonic()
    remote_deadline = (
        start + expected_seconds + REMOTE_KILL_GRACE_S
        if (expected_seconds is not None and is_gem)
        else None
    )
    local_deadline = (
        start + expected_seconds + LOCAL_KILL_GRACE_S
        if expected_seconds is not None
        else None
    )
    remote_kill_thread: threading.Thread | None = None

    while True:
        rc = proc.poll()
        if rc is not None:
            # Process finished on its own. If we already launched the remote
            # kill, let that thread finish in the background -- it's fine to
            # let it complete after we return.
            return rc

        now = time.monotonic()

        # Stage 1: gem-mode remote kill at seconds + 10.
        if (
            remote_deadline is not None
            and remote_kill_thread is None
            and now >= remote_deadline
        ):
            log.warning(
                "sbk-gem-yal: %ds + %ds grace reached; killing remote sbk "
                "clients on all nodes",
                expected_seconds, int(REMOTE_KILL_GRACE_S),
            )
            remote_kill_thread = threading.Thread(
                target=_kill_remote_sbk_clients,
                args=(yml_path,),
                daemon=True,
            )
            remote_kill_thread.start()

        # Stage 2: local kill at seconds + 15.
        if local_deadline is not None and now >= local_deadline:
            # For gem mode, wait briefly for the remote-kill thread so the
            # remote SBKs are gone *before* we kill the local sbk-gem-yal.
            if remote_kill_thread is not None:
                remote_kill_thread.join(timeout=5)
            try:
                size = csv_path.stat().st_size if csv_path.exists() else 0
            except OSError:
                size = 0
            log.warning(
                "%s instance did not complete within %ds + %ds; killing "
                "local process (csv=%d bytes)",
                "sbk-gem-yal" if is_gem else "sbk-yal",
                expected_seconds, int(LOCAL_KILL_GRACE_S), size,
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
        _, is_gem = _read_yml(yml_path)
        seconds = _expected_seconds(yml_path)
        log.info(
            "[serial] starting class=%s mode=%s yml=%s expected_seconds=%s",
            class_name,
            "sbk-gem-yal" if is_gem else "sbk-yal",
            yml_path,
            seconds,
        )
        start = time.monotonic()
        # stdout/stderr inherited so the user sees output live
        proc = subprocess.Popen(_build_cmd(executable, yml_path))
        rc = _hung_jvm_watchdog(
            proc, csv_path, yml_path,
            expected_seconds=seconds, is_gem=is_gem,
        )
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

    # Each tuple:
    #   (class_name, yml, csv, log, popen, start_ts,
    #    expected_seconds, remote_deadline, local_deadline, is_gem)
    procs: list[
        tuple[
            str, Path, Path, Path, subprocess.Popen, float,
            int | None, float | None, float | None, bool,
        ]
    ] = []
    remote_kill_threads: dict[int, threading.Thread] = {}

    for class_name, yml_path, csv_path in jobs:
        log_path = log_dir / f"sbk-{class_name}.log"
        f = log_path.open("w")
        _, is_gem = _read_yml(yml_path)
        seconds = _expected_seconds(yml_path)
        log.info(
            "[parallel] launching class=%s mode=%s log=%s expected_seconds=%s",
            class_name,
            "sbk-gem-yal" if is_gem else "sbk-yal",
            log_path, seconds,
        )
        start = time.monotonic()
        p = subprocess.Popen(
            _build_cmd(executable, yml_path),
            stdout=f,
            stderr=subprocess.STDOUT,
        )
        remote_dl = (
            start + seconds + REMOTE_KILL_GRACE_S
            if (seconds is not None and is_gem)
            else None
        )
        local_dl = (
            start + seconds + LOCAL_KILL_GRACE_S
            if seconds is not None
            else None
        )
        procs.append(
            (class_name, yml_path, csv_path, log_path, p, start,
             seconds, remote_dl, local_dl, is_gem)
        )

    # heartbeat loop
    pending = {i for i in range(len(procs))}
    last_print = 0.0
    HEARTBEAT = 5.0
    while pending:
        time.sleep(0.5)
        now = time.monotonic()
        for i in list(pending):
            (class_name, yml_path, csv_path, _, p, p_start,
             seconds, remote_dl, local_dl, is_gem) = procs[i]
            if p.poll() is not None:
                pending.discard(i)
                continue

            # Stage 1: gem-mode remote kill at seconds + 10.
            if (
                remote_dl is not None
                and i not in remote_kill_threads
                and now >= remote_dl
            ):
                log.warning(
                    "[parallel] class=%s sbk-gem-yal: %ds + %ds grace reached; "
                    "killing remote sbk clients",
                    class_name, seconds, int(REMOTE_KILL_GRACE_S),
                )
                t = threading.Thread(
                    target=_kill_remote_sbk_clients, args=(yml_path,),
                    daemon=True,
                )
                t.start()
                remote_kill_threads[i] = t

            # Stage 2: local kill at seconds + 15.
            if local_dl is not None and now >= local_dl:
                t = remote_kill_threads.get(i)
                if t is not None:
                    t.join(timeout=5)
                size = csv_path.stat().st_size if csv_path.exists() else 0
                log.warning(
                    "[parallel] class=%s (%s) did not complete within %ds + "
                    "%ds; killing local process (csv=%d bytes)",
                    class_name,
                    "sbk-gem-yal" if is_gem else "sbk-yal",
                    seconds, int(LOCAL_KILL_GRACE_S), size,
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
    for class_name, yml_path, csv_path, log_path, p, start, *_ in procs:
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
