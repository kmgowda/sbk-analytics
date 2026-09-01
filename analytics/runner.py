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

from .policy import RUNTIME_POLICY, SBK_ARTIFACT
from .processes import (
    ManagedProcess,
    managed_popen,
    terminate_all,
    terminate_process,
)

log = logging.getLogger(__name__)
BENCHMARK_POLICY = RUNTIME_POLICY.benchmarks
ENVIRONMENT_POLICY = RUNTIME_POLICY.environment
LAYOUT_POLICY = RUNTIME_POLICY.dependency_layout
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface
DISPLAY_POLICY = RUNTIME_POLICY.display

PARALLEL_WARNING = (
    "WARNING: parallel mode is experimental. Multiple SBK instances will run "
    "concurrently against potentially shared storage backends, which may distort "
    "benchmark results. Per-instance stdout/stderr is captured to log files; only "
    "a progress heartbeat is printed every "
    f"{BENCHMARK_POLICY.heartbeat_interval_s:g} seconds."
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
        """SBK must exit successfully and produce a non-empty CSV."""
        return (
            self.returncode == 0
            and self.csv_path.exists()
            and self.csv_path.stat().st_size > 0
        )


def _build_cmd(executable: Path, yml_path: Path) -> list[str]:
    return [str(executable), "-f", str(yml_path)]


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
    binary = (
        SBK_ARTIFACT.additional_executables[0]
        if is_gem else SBK_ARTIFACT.primary_executable
    )
    params, _ = _read_yml(yml_path)
    timeout_desc = "SBK native lifecycle"
    if expected_seconds is not None:
        timeout_desc += f" (benchmark duration: {expected_seconds}s)"
    elif is_gem:
        timeout_desc += " (deployment time excluded from benchmark timing)"

    mode_tag = "serial" if serial else "parallel"
    lines = [
        "",
        "=" * DISPLAY_POLICY.section_width,
        f"  [{mode_tag}] LAUNCHING {binary.upper()} instance "
        f"({instance_index} of {instance_total}): {instance_name}",
        "=" * DISPLAY_POLICY.section_width,
        f"  executable : {cmd[0]}",
        f"  command    : {' '.join(cmd)}",
        f"  yaml       : {yml_path}",
        f"  csv (out)  : {csv_path}",
    ]
    if log_path:
        lines.append(f"  log file   : {log_path}")
    if env is not None:
        if ENVIRONMENT_POLICY.sbk_java_home in env:
            lines.append(
                f"  {ENVIRONMENT_POLICY.sbk_java_home} : "
                f"{env[ENVIRONMENT_POLICY.sbk_java_home]}"
            )
        if ENVIRONMENT_POLICY.java_home in env:
            lines.append(
                f"  {ENVIRONMENT_POLICY.java_home}     : "
                f"{env[ENVIRONMENT_POLICY.java_home]}"
            )
    lines.append(f"  timeout    : {timeout_desc}")
    wrapper = (
        SBK_INTERFACE_POLICY.gem_arguments_wrapper
        if is_gem else SBK_INTERFACE_POLICY.local_arguments_wrapper
    )
    lines.append(f"  -- {binary} arguments ({wrapper}:) --")
    for k, v in params.items():
        lines.append(f"    {k}: {v}")
    lines.append("=" * DISPLAY_POLICY.section_width)
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
    gem_wrapper = SBK_INTERFACE_POLICY.gem_arguments_wrapper
    local_wrapper = SBK_INTERFACE_POLICY.local_arguments_wrapper
    if isinstance(data.get(gem_wrapper), dict):
        return data[gem_wrapper], True
    if isinstance(data.get(local_wrapper), dict):
        return data[local_wrapper], False
    return {}, False


def _expected_seconds(yml_path: Path) -> int | None:
    """Read the benchmark's ``seconds:`` setting out of an SBK YAL/GEM-YAL YAML.

    Returns the configured value when it is a positive integer. Returns ``None``
    in any of the following cases (which all mean *no timeout*):

    - ``seconds:`` is not set in the YAML
    - ``seconds:`` is set to a non-positive value (``0`` / negative)
    - the YAML cannot be parsed

    Analytics uses this only for display. SBK owns benchmark timing,
    fixed-record idle detection, and normal process completion.
    """
    params, _ = _read_yml(yml_path)
    seconds_option = SBK_INTERFACE_POLICY.seconds_option
    if seconds_option not in params:
        return None
    try:
        secs = int(params[seconds_option])
    except (TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _lifecycle_metadata(
    class_name: str, yml_path: Path, *, is_gem: bool
) -> dict[str, object]:
    """Return credential-free ownership details for durable diagnostics."""
    metadata: dict[str, object] = {
        "instance": class_name,
        "yaml": str(yml_path),
    }
    if is_gem:
        params, _ = _read_yml(yml_path)
        nodes_raw = params.get(SBK_INTERFACE_POLICY.nodes_option)
        if isinstance(nodes_raw, (list, tuple)):
            nodes = [str(node).strip() for node in nodes_raw if str(node).strip()]
        else:
            value = str(nodes_raw or "")
            for separator in (",", "\n", "\t"):
                value = value.replace(separator, " ")
            nodes = [node for node in value.split() if node]
        metadata["remote_nodes"] = nodes
        metadata["remote_cleanup_owner"] = RUNTIME_POLICY.lifecycle.gem_role
    return metadata


def _wait_for_native_completion(proc: ManagedProcess) -> int:
    """Wait for SBK to report its authoritative terminal status."""
    return proc.wait()


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
        if ENVIRONMENT_POLICY.java_tool_options in env_unbuffered:
            java_opts.append(
                env_unbuffered[ENVIRONMENT_POLICY.java_tool_options]
            )
        java_opts.extend([
            '-Djava.stdout.buffered=false',
            '-Djava.stderr.buffered=false',
            '-Dsun.stdout.encoding=UTF-8',
            '-Dsun.stderr.encoding=UTF-8'
        ])
        env_unbuffered[ENVIRONMENT_POLICY.java_tool_options] = ' '.join(java_opts)
        
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
                lifecycle_role=(
                    RUNTIME_POLICY.lifecycle.gem_role
                    if is_gem else RUNTIME_POLICY.lifecycle.local_role
                ),
                lifecycle_metadata=_lifecycle_metadata(
                    class_name, yml_path, is_gem=is_gem
                ),
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
                lifecycle_role=(
                    RUNTIME_POLICY.lifecycle.gem_role
                    if is_gem else RUNTIME_POLICY.lifecycle.local_role
                ),
                lifecycle_metadata=_lifecycle_metadata(
                    class_name, yml_path, is_gem=is_gem
                ),
            )
        try:
            rc = _wait_for_native_completion(proc)
        except BaseException:
            _terminate_sbk_process(proc, yml_path, is_gem=is_gem)
            raise
        finally:
            if use_forwarding:
                forward_thread.join(
                    timeout=BENCHMARK_POLICY.log_forward_join_s
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
    *,
    env: dict[str, str] | None = None,
    executables: dict[str, Path] | None = None,
) -> list[RunResult]:
    print(PARALLEL_WARNING, file=sys.stderr, flush=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    # Each tuple: (class_name, yml, csv, log, popen, start_ts, is_gem)
    procs: list[
        tuple[
            str, Path, Path, Path, ManagedProcess, float,
            bool,
        ]
    ] = []

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
                lifecycle_role=(
                    RUNTIME_POLICY.lifecycle.gem_role
                    if is_gem else RUNTIME_POLICY.lifecycle.local_role
                ),
                lifecycle_metadata=_lifecycle_metadata(
                    class_name, yml_path, is_gem=is_gem
                ),
            )
        finally:
            # Popen duplicates the descriptor for the child; the parent should
            # not keep one log file open for every parallel benchmark.
            f.close()
        procs.append(
            (class_name, yml_path, csv_path, log_path, p, start, is_gem)
        )

    # heartbeat loop
    pending = {i for i in range(len(procs))}
    last_print = 0.0
    try:
        while pending:
            time.sleep(BENCHMARK_POLICY.process_poll_interval_s)
            now = time.monotonic()
            for i in list(pending):
                (_, _, _, _, p, _, _) = procs[i]
                if p.poll() is not None:
                    pending.discard(i)
            if (
                pending
                and (now - last_print)
                >= BENCHMARK_POLICY.heartbeat_interval_s
            ):
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
            _, yml_path, _, _, process, _, is_gem
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
    """Let SBK-GEM own remote cleanup before local force termination."""
    if process.poll() is not None:
        return
    if is_gem:
        log.warning(
            "requesting native SBK-GEM shutdown; allowing %.0fs for remote cleanup",
            BENCHMARK_POLICY.gem_native_shutdown_grace_s,
        )
        process.terminate()
        try:
            process.wait(timeout=BENCHMARK_POLICY.gem_native_shutdown_grace_s)
            return
        except subprocess.TimeoutExpired:
            log.warning(
                "SBK-GEM did not finish native cleanup; forcing only its locally "
                "owned process group. Remote process-name killing is intentionally "
                "disabled because it cannot distinguish concurrent SBK runs"
            )
    terminate_process(process)


def _sbk_env(jdk_home: Path | None) -> dict[str, str] | None:
    """Return the subprocess env for sbk-yal / sbk-gem-yal.

    Sets ``SBK_JAVA_HOME`` (preferred by SBK) to the specified JDK.
    Explicitly unsets ``JAVA_HOME`` if it exists in the parent environment to prevent
    SBK from using a different Java version. SBK uses SBK_JAVA_HOME if set.
    Also prepends ``<jdk>/bin`` to ``PATH``.
    Returns None when no JDK is supplied so the SBK scripts fall back to the
    caller's existing environment.
    """
    if jdk_home is None:
        return None
    env = os.environ.copy()
    jdk_home_str = str(jdk_home)
    # Set the launcher-specific Java home for SBK.
    env[ENVIRONMENT_POLICY.sbk_java_home] = jdk_home_str
    # Explicitly unset JAVA_HOME to prevent SBK from using a different Java version
    # SBK uses SBK_JAVA_HOME if set, otherwise it falls back to JAVA_HOME.
    if ENVIRONMENT_POLICY.java_home in env:
        del env[ENVIRONMENT_POLICY.java_home]
    # Prepend JDK bin to PATH
    env[ENVIRONMENT_POLICY.path] = (
        f"{jdk_home / LAYOUT_POLICY.executable_directory}{os.pathsep}"
        f"{env.get(ENVIRONMENT_POLICY.path, '')}"
    )
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
