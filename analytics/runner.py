#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
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

from .processes import (
    ManagedProcess,
    managed_popen,
    terminate_all,
    terminate_process,
)

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


def _kill_grace_for(yml_path: Path) -> tuple[int, int]:
    """Return ``(remote_grace, local_grace)`` for the YAML's mode."""
    _, is_gem = _read_yml(yml_path)
    if is_gem:
        return int(REMOTE_KILL_GRACE_S), int(LOCAL_KILL_GRACE_S)
    return 0, int(LOCAL_KILL_GRACE_S)


def _print_sbk_banner(
    *,
    instance_name: str,
    is_gem: bool,
    yml_path: Path,
    csv_path: Path,
    cmd: list[str],
    env: dict[str, str] | None,
    expected_seconds: int | None,
    serial: bool,
    instance_index: int,
    instance_total: int,
    log_path: Path | None = None,
) -> None:
    """Print a human-readable banner with the full SBK invocation details."""
    binary = "sbk-gem-yal" if is_gem else "sbk-yal"
    params, _ = _read_yml(yml_path)
    if expected_seconds is None:
        timeout_desc = "no timeout (records-bounded / open-ended)"
    elif is_gem:
        timeout_desc = (
            f"remote kill at +{int(REMOTE_KILL_GRACE_S)}s, "
            f"local kill at +{int(LOCAL_KILL_GRACE_S)}s "
            f"(deadline {expected_seconds + int(LOCAL_KILL_GRACE_S)}s)"
        )
    else:
        timeout_desc = (
            f"kill at seconds + {int(LOCAL_KILL_GRACE_S)}s "
            f"(deadline {expected_seconds + int(LOCAL_KILL_GRACE_S)}s)"
        )

    mode_tag = "serial" if serial else "parallel"
    lines = [
        "",
        "=" * 78,
        f"  [{mode_tag}] LAUNCHING {binary.upper()} instance "
        f"({instance_index} of {instance_total}): {instance_name}",
        "=" * 78,
        f"  executable : {cmd[0]}",
        f"  command    : {' '.join(cmd)}",
        f"  yaml       : {yml_path}",
        f"  csv (out)  : {csv_path}",
    ]
    if log_path:
        lines.append(f"  log file   : {log_path}")
    if env is not None:
        if "SBK_JAVA_HOME" in env:
            lines.append(f"  SBK_JAVA_HOME : {env['SBK_JAVA_HOME']}")
        if "JAVA_HOME" in env:
            lines.append(f"  JAVA_HOME     : {env['JAVA_HOME']}")
    lines.append(f"  timeout    : {timeout_desc}")
    wrapper = "sbkGemArgs" if is_gem else "sbkArgs"
    lines.append(f"  -- {binary} arguments ({wrapper}:) --")
    for k, v in params.items():
        lines.append(f"    {k}: {v}")
    lines.append("=" * 78)
    # Print banner unconditionally (independent of -v / log level); these are
    # status messages, not debug logs.
    print("\n".join(lines), file=sys.stderr, flush=True)


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
    log.warning(
        "remote sbk kill is best effort and insecure: SSH host-key checking "
        "is disabled, and every remote process matching %r will be killed",
        _remote_kill_pattern(),
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
    proc: ManagedProcess,
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
    *,
    env: dict[str, str] | None = None,
    forward_logs: bool = False,
    executables: dict[str, Path] | None = None,
    output_to_stderr: bool = False,
) -> list[RunResult]:
    results: list[RunResult] = []
    total = len(jobs)
    for idx, (class_name, yml_path, csv_path) in enumerate(jobs, start=1):
        job_executable = (executables or {}).get(class_name, executable)
        _, is_gem = _read_yml(yml_path)
        seconds = _expected_seconds(yml_path)
        cmd = _build_cmd(job_executable, yml_path)
        _print_sbk_banner(
            instance_name=class_name,
            is_gem=is_gem,
            yml_path=yml_path,
            csv_path=csv_path,
            cmd=cmd,
            env=env,
            expected_seconds=seconds,
            serial=True,
            instance_index=idx,
            instance_total=total,
        )
        start = time.monotonic()
        # stdout/stderr inherited so the user sees output live
        # On macOS, we need to explicitly handle Java output to ensure logs are visible
        env_unbuffered = env.copy() if env is not None else os.environ.copy()
        
        # Force Java to use unbuffered stdout/stderr (important for macOS)
        java_opts = []
        if 'JAVA_TOOL_OPTIONS' in env_unbuffered:
            java_opts.append(env_unbuffered['JAVA_TOOL_OPTIONS'])
        java_opts.extend([
            '-Djava.stdout.buffered=false',
            '-Djava.stderr.buffered=false',
            '-Dsun.stdout.encoding=UTF-8',
            '-Dsun.stderr.encoding=UTF-8'
        ])
        env_unbuffered['JAVA_TOOL_OPTIONS'] = ' '.join(java_opts)
        
        # On macOS or when forced, explicitly capture and forward output to ensure visibility
        use_forwarding = sys.platform == 'darwin' or forward_logs
        
        if use_forwarding:
            log.debug("Using explicit output forwarding for SBK logs")
            proc = managed_popen(
                cmd,
                env=env_unbuffered,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,  # Line buffered
                text=True,
                universal_newlines=True,
            )
            # Forward output in real-time
            def forward_output():
                try:
                    for line in proc.stdout:
                        print(
                            line, end='', flush=True,
                            file=sys.stderr if output_to_stderr else sys.stdout,
                        )
                except:
                    pass
            
            forward_thread = threading.Thread(target=forward_output, daemon=True)
            forward_thread.start()
        else:
            log.debug("Using default subprocess configuration")
            proc = managed_popen(
                cmd,
                env=env_unbuffered,
                stdout=sys.stderr if output_to_stderr else None,
                stderr=sys.stderr if output_to_stderr else None,
            )
        try:
            rc = _hung_jvm_watchdog(
                proc, csv_path, yml_path,
                expected_seconds=seconds, is_gem=is_gem,
            )
        except BaseException:
            _terminate_sbk_process(proc, yml_path, is_gem=is_gem)
            raise
        finally:
            if use_forwarding:
                forward_thread.join(timeout=1)
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
    *,
    env: dict[str, str] | None = None,
    executables: dict[str, Path] | None = None,
) -> list[RunResult]:
    print(PARALLEL_WARNING, file=sys.stderr, flush=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Each tuple:
    #   (class_name, yml, csv, log, popen, start_ts,
    #    expected_seconds, remote_deadline, local_deadline, is_gem)
    procs: list[
        tuple[
            str, Path, Path, Path, ManagedProcess, float,
            int | None, float | None, float | None, bool,
        ]
    ] = []
    remote_kill_threads: dict[int, threading.Thread] = {}

    total = len(jobs)
    for idx, (class_name, yml_path, csv_path) in enumerate(jobs, start=1):
        job_executable = (executables or {}).get(class_name, executable)
        log_path = log_dir / f"sbk-{class_name}.log"
        f = log_path.open("w")
        _, is_gem = _read_yml(yml_path)
        seconds = _expected_seconds(yml_path)
        cmd = _build_cmd(job_executable, yml_path)
        _print_sbk_banner(
            instance_name=class_name,
            is_gem=is_gem,
            yml_path=yml_path,
            csv_path=csv_path,
            cmd=cmd,
            env=env,
            expected_seconds=seconds,
            serial=False,
            instance_index=idx,
            instance_total=total,
            log_path=log_path,
        )
        start = time.monotonic()
        try:
            p = managed_popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                env=env,
            )
        finally:
            # Popen duplicates the descriptor for the child; the parent should
            # not keep one log file open for every parallel benchmark.
            f.close()
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
    try:
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
                        "[parallel] class=%s sbk-gem-yal: %ds + %ds grace "
                        "reached; killing remote sbk clients",
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
                        "[parallel] class=%s (%s) did not complete within "
                        "%ds + %ds; killing local process (csv=%d bytes)",
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
                elapsed = [
                    f"{procs[i][0]}={now - procs[i][5]:.0f}s" for i in pending
                ]
                print(
                    f"[parallel] {len(running)} running: {', '.join(elapsed)}",
                    file=sys.stderr,
                    flush=True,
                )
                last_print = now
    except BaseException:
        for (
            _, yml_path, _, _, process, _, _, _, _, is_gem
        ) in procs:
            _terminate_sbk_process(process, yml_path, is_gem=is_gem)
        raise

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


def _terminate_sbk_process(
    process: ManagedProcess, yml_path: Path, *, is_gem: bool
) -> None:
    """Terminate one SBK tree and best-effort remote GEM clients."""
    if process.poll() is not None:
        return
    if is_gem:
        _kill_remote_sbk_clients(yml_path)
    terminate_process(process)


def _sbk_env(jdk_home: Path | None) -> dict[str, str] | None:
    """Return the subprocess env for sbk-yal / sbk-gem-yal.

    Sets ``SBK_JAVA_HOME`` (preferred by SBK 10.0) to the specified JDK.
    Explicitly unsets ``JAVA_HOME`` if it exists in the parent environment to prevent
    SBK from using a different Java version. SBK 10.0 will use SBK_JAVA_HOME if set.
    Also prepends ``<jdk>/bin`` to ``PATH``.
    Returns None when no JDK is supplied so the SBK scripts fall back to the
    caller's existing environment.
    """
    if jdk_home is None:
        return None
    env = os.environ.copy()
    jdk_home_str = str(jdk_home)
    # Set SBK_JAVA_HOME for SBK 10.0
    env["SBK_JAVA_HOME"] = jdk_home_str
    # Explicitly unset JAVA_HOME to prevent SBK from using a different Java version
    # SBK 10.0 will use SBK_JAVA_HOME if set, otherwise fall back to JAVA_HOME
    if "JAVA_HOME" in env:
        del env["JAVA_HOME"]
    # Prepend JDK bin to PATH
    env["PATH"] = f"{jdk_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    return env


def run_jobs(
    executable: Path,
    jobs: list[tuple[str, Path, Path]],
    *,
    mode: str,
    log_dir: Path,
    jdk_home: Path | None = None,
    forward_logs: bool = False,
    executables: dict[str, Path] | None = None,
    output_to_stderr: bool = False,
) -> list[RunResult]:
    """Run benchmark jobs with an optional executable override per instance.

    ``executable`` remains the default for backwards compatibility. Mixed
    local/GEM plans pass ``executables`` keyed by the unique instance name.
    """
    required_executables = {
        (executables or {}).get(class_name, executable)
        for class_name, _, _ in jobs
    }
    for required_executable in required_executables:
        if not required_executable.exists():
            raise FileNotFoundError(
                f"SBK executable not found: {required_executable}"
            )
    env = _sbk_env(jdk_home)
    try:
        if mode == "parallel":
            return _run_parallel(
                executable, jobs, log_dir, env=env, executables=executables
            )
        return _run_serial(
            executable,
            jobs,
            env=env,
            forward_logs=forward_logs,
            executables=executables,
            output_to_stderr=output_to_stderr,
        )
    except BaseException:
        # Covers interruption during process creation before the new process
        # has been appended to the serial/parallel runner's local collection.
        terminate_all()
        raise
