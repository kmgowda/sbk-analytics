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
* Windows children get a new process group and are assigned to a Job Object
  configured with KILL_ON_JOB_CLOSE, so closing the parent kills descendants.
* Catchable termination signals unwind Python normally, allowing runner-level
  cleanup (including sbk-gem remote cleanup) before the registry escalates.
"""
from __future__ import annotations

import atexit
import contextlib
import ctypes
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Iterator

log = logging.getLogger(__name__)

TERMINATE_GRACE_S = 3.0
_ACTIVE: set["ManagedProcess"] = set()
_ACTIVE_LOCK = threading.RLock()


class ProcessExit(SystemExit):
    """Exit caused by a catchable operating-system termination signal."""

    def __init__(self, signum: int):
        self.signum = signum
        super().__init__(128 + signum)


class ManagedProcess:
    """Small ``subprocess.Popen`` facade that controls the whole child tree."""

    def __init__(
        self,
        process: subprocess.Popen,
        *,
        guard_write_fd: int | None = None,
        guard_process: subprocess.Popen | None = None,
        windows_job: int | None = None,
    ) -> None:
        self._process = process
        self._guard_write_fd = guard_write_fd
        self._guard_process = guard_process
        self._windows_job = windows_job
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
        if os.name == "nt":
            try:
                self._process.send_signal(signal.CTRL_BREAK_EVENT)
            except (OSError, ValueError):
                self._process.terminate()
            return
        _signal_posix_group(self.pid, signal.SIGTERM)

    def kill(self) -> None:
        """Force-kill the entire workload tree."""
        if self._process.poll() is not None:
            self._finish()
            return
        if os.name == "nt":
            if self._windows_job is not None:
                _close_windows_handle(self._windows_job)
                self._windows_job = None
            else:
                _taskkill_tree(self.pid)
            return
        _signal_posix_group(self.pid, signal.SIGKILL)

    def send_signal(self, signum: int) -> None:
        if os.name == "nt":
            self._process.send_signal(signum)
        else:
            _signal_posix_group(self.pid, signum)

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        with _ACTIVE_LOCK:
            _ACTIVE.discard(self)
        self._disarm_guard()
        if self._windows_job is not None:
            _close_windows_handle(self._windows_job)
            self._windows_job = None

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
                guard.wait(timeout=TERMINATE_GRACE_S + 1)
            except subprocess.TimeoutExpired:
                guard.terminate()
                try:
                    guard.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    guard.kill()
                    guard.wait()


def managed_popen(args, **kwargs: Any) -> ManagedProcess:
    """Start a workload whose complete descendant tree is lifecycle-managed."""
    if os.name == "nt":
        flags = int(kwargs.pop("creationflags", 0))
        flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(args, creationflags=flags, **kwargs)
        managed = ManagedProcess(process)
        managed._windows_job = _create_windows_kill_job(process)
        managed._guard_process = _start_windows_guard(process.pid)
        return managed

    kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    managed = ManagedProcess(process)
    read_fd: int | None = None
    write_fd: int | None = None
    guard: subprocess.Popen | None = None
    try:
        read_fd, write_fd = os.pipe()
        guard = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "analytics._process_guard",
                str(read_fd),
                str(process.pid),
                str(TERMINATE_GRACE_S),
            ],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        os.close(read_fd)
    except Exception as exc:
        log.warning("could not start parent-death process guard: %s", exc)
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
        write_fd = None
        guard = None
    managed._guard_write_fd = write_fd
    managed._guard_process = guard
    return managed


def terminate_process(process: ManagedProcess, grace_s: float = TERMINATE_GRACE_S) -> int | None:
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


def terminate_all(grace_s: float = TERMINATE_GRACE_S) -> None:
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
def child_process_cleanup() -> Iterator[None]:
    """Install CLI signal handling and always clean up registered workloads."""
    previous: dict[int, Any] = {}

    def _handle(signum, _frame) -> None:
        # Raising allows runner.py to perform sbk-gem remote cleanup before the
        # context's final registry sweep terminates any remaining local trees.
        raise ProcessExit(signum)

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT", "SIGBREAK"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handle)
    try:
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


def _create_windows_kill_job(process: subprocess.Popen) -> int | None:
    """Assign ``process`` to a KILL_ON_JOB_CLOSE Windows Job Object."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class BASIC_LIMITS(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMITS(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMITS),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        log.warning("CreateJobObject failed: %s", ctypes.get_last_error())
        return None
    limits = EXTENDED_LIMITS()
    limits.BasicLimitInformation.LimitFlags = 0x00002000
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        log.warning("SetInformationJobObject failed: %s", ctypes.get_last_error())
        _close_windows_handle(int(job))
        return None
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(process._handle)):
        log.warning("AssignProcessToJobObject failed: %s", ctypes.get_last_error())
        _close_windows_handle(int(job))
        return None
    return int(job)


def _start_windows_guard(target_pid: int) -> subprocess.Popen | None:
    """Start a parent/target handle watcher as a Job Object fallback."""
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "analytics._process_guard",
                "--windows",
                str(os.getpid()),
                str(target_pid),
                str(TERMINATE_GRACE_S),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as exc:
        log.warning("could not start Windows parent-death guard: %s", exc)
        return None


def _close_windows_handle(handle: int) -> None:
    if os.name == "nt" and handle:
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _taskkill_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        pass


atexit.register(terminate_all)
