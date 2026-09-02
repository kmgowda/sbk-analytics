#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Managed subprocess trees for benchmark workloads.

SBK launchers and sbk-charts may create shells, JVMs, and other descendants.
This module gives every workload its own process tree and keeps it tied to the
sbk-analytics lifetime:

* POSIX children get a new session/process group.  A small independent guard
  watches a parent-owned pipe and terminates the group if the parent vanishes,
  including an uncatchable SIGKILL.
* Catchable termination signals unwind Python normally, allowing SBK-GEM's
  native shutdown before the registry escalates local process termination.
"""
from __future__ import annotations

import atexit
import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .errors import LifecycleError
from .lifecycle import (
    current_run_id,
    reconcile_stale_records,
    record_path,
    register_process,
    unregister_process,
)
from .policy import RUNTIME_POLICY

log = logging.getLogger(__name__)

PROCESS_POLICY = RUNTIME_POLICY.processes
EXIT_CODE_POLICY = RUNTIME_POLICY.exit_codes
_ACTIVE: set["ManagedProcess"] = set()
_ACTIVE_LOCK = threading.RLock()


class ProcessExit(SystemExit):
    """Exit caused by a catchable operating-system termination signal."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(EXIT_CODE_POLICY.signal_base + signum)


class ManagedProcess:
    """Small ``subprocess.Popen`` facade that controls the whole child tree."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        guard_write_fd: int | None = None,
        guard_process: subprocess.Popen | None = None,
        lifecycle_record: os.PathLike[str] | None = None,
    ) -> None:
        self._process = process
        self._guard_write_fd = guard_write_fd
        self._guard_process = guard_process
        self._lifecycle_record = (
            os.fspath(lifecycle_record) if lifecycle_record is not None else None
        )
        self._finished = False
        with _ACTIVE_LOCK:
            _ACTIVE.add(self)

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stdout(self):
        return self._process.stdout

    @property
    def stderr(self):
        return self._process.stderr

    def poll(self) -> int | None:
        rc = self._process.poll()
        if rc is not None:
            self._finish()
        return rc

    def wait(self, timeout: float | None = None) -> int:
        rc = self._process.wait(timeout=timeout)
        self._finish()
        return rc

    def terminate(self) -> None:
        """Request graceful termination of the entire workload tree."""
        if self._process.poll() is not None:
            self._finish()
            return
        _signal_posix_group(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        """Force-kill the entire workload tree."""
        if self._process.poll() is not None:
            self._finish()
            return
        _signal_posix_group(self.pid, signal.SIGKILL)

    def send_signal(self, signum: int) -> None:
        _signal_posix_group(self.pid, signum)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)
        self._disarm_guard()

    def _disarm_guard(self) -> None:
        fd, guard = self._guard_write_fd, self._guard_process
        self._guard_write_fd = None
        self._guard_process = None
        if fd is not None:
            try:
                # The direct child has exited. Ask the guard to remove any
                # descendants that detached from the wrapper before exiting.
                os.write(fd, b"K")
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        if guard is not None:
            try:
                guard.wait(
                    timeout=(
                        PROCESS_POLICY.termination_grace_s
                        + PROCESS_POLICY.guard_exit_padding_s
                    )
                )
            except subprocess.TimeoutExpired:
                guard.terminate()
                try:
                    guard.wait(timeout=PROCESS_POLICY.guard_force_wait_s)
                except subprocess.TimeoutExpired:
                    guard.kill()
                    guard.wait()
        if not _process_group_exists(self.pid):
            unregister_process(
                None
                if self._lifecycle_record is None
                else Path(self._lifecycle_record)
            )
        self._lifecycle_record = None


def managed_popen(args, **kwargs: Any) -> ManagedProcess:
    """Start a workload whose complete descendant tree is lifecycle-managed."""
    role = kwargs.pop("lifecycle_role", RUNTIME_POLICY.lifecycle.local_role)
    metadata = kwargs.pop("lifecycle_metadata", None)
    child_env = (
        kwargs["env"].copy() if kwargs.get("env") is not None else os.environ.copy()
    )
    child_env[RUNTIME_POLICY.environment.lifecycle_run_id] = current_run_id()
    kwargs["env"] = child_env
    kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    read_fd: int | None = None
    write_fd: int | None = None
    guard: subprocess.Popen | None = None
    lifecycle_record = record_path(process.pid)
    try:
        read_fd, write_fd = os.pipe()
        guard = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "analytics._process_guard",
                str(read_fd),
                str(process.pid),
                str(PROCESS_POLICY.termination_grace_s),
                str(lifecycle_record),
            ],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        os.close(read_fd)
        read_fd = None
        if process.poll() is None:
            register_process(
                process.pid,
                args,
                role=role,
                metadata=metadata,
                path=lifecycle_record,
            )
        else:
            # Very short commands can finish between Popen and registration.
            # Their already-running guard still owns any surviving process
            # group descendants; there is no live PID identity to persist.
            lifecycle_record = None
    except Exception as exc:
        log.error("could not establish durable workload ownership: %s", exc)
        if read_fd is not None:
            try:
                os.close(read_fd)
            except OSError:
                pass
        if write_fd is not None:
            try:
                os.close(write_fd)
            except OSError:
                pass
        _signal_posix_group(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=PROCESS_POLICY.guard_force_wait_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if guard is not None:
            try:
                guard.wait(timeout=PROCESS_POLICY.guard_force_wait_s)
            except subprocess.TimeoutExpired:
                guard.kill()
                guard.wait()
        unregister_process(lifecycle_record)
        raise LifecycleError(
            "failed to start the mandatory workload lifecycle guard"
        ) from exc
    return ManagedProcess(
        process,
        guard_write_fd=write_fd,
        guard_process=guard,
        lifecycle_record=lifecycle_record,
    )


def terminate_process(
    process: ManagedProcess,
    grace_s: float = PROCESS_POLICY.termination_grace_s,
) -> int | None:
    """Terminate one workload tree, escalating to a force-kill after grace."""
    if process.poll() is not None:
        return process.returncode
    log.warning("terminating workload process tree pid=%s", process.pid)
    process.terminate()
    try:
        return process.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            return process.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            log.error("workload tree pid=%s did not exit after force-kill", process.pid)
            return process.poll()


def terminate_all(
    grace_s: float = PROCESS_POLICY.termination_grace_s,
) -> None:
    """Best-effort termination of every workload still owned by this process."""
    with _ACTIVE_LOCK:
        active = list(_ACTIVE)
    if not active:
        return
    log.warning("terminating %d active workload process tree(s)", len(active))
    for process in active:
        try:
            process.terminate()
        except Exception as exc:
            log.warning("failed to terminate workload pid=%s: %s", process.pid, exc)
    deadline = time.monotonic() + grace_s
    for process in active:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=grace_s)
            except Exception as exc:
                log.warning("failed to kill workload pid=%s: %s", process.pid, exc)


@contextlib.contextmanager
def child_process_cleanup(*, reconcile: bool = True) -> Iterator[None]:
    """Install CLI signal handling and always clean up registered workloads."""
    previous: dict[int, Any] = {}

    def _handle(signum, _frame) -> None:
        # Raising allows runner.py to request SBK-GEM's native cleanup before
        # the context's final registry sweep terminates remaining local trees.
        raise ProcessExit(signum)

    if threading.current_thread() is threading.main_thread():
        for name in PROCESS_POLICY.handled_signal_names:
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle)
    try:
        if reconcile:
            summary = reconcile_stale_records()
            lifecycle = RUNTIME_POLICY.lifecycle
            if (
                summary[lifecycle.cleaned_field]
                or summary[lifecycle.expired_field]
                or summary[lifecycle.unresolved_field]
            ):
                log.info("lifecycle reconciliation: %s", summary)
        yield
    finally:
        terminate_all()
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _signal_posix_group(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        # Cleanup is best effort: one inaccessible group must not prevent the
        # registry sweep from attempting every other managed workload.
        log.warning(
            "permission denied signalling workload process group pgid=%s "
            "signal=%s: %s",
            pgid,
            signum,
            exc,
        )


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

atexit.register(terminate_all)
