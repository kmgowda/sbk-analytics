#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""POSIX companion that kills a workload group when its parent disappears."""
from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _kill_group(pgid: int, grace_s: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--windows"]:
        return _windows_main(args[1:])
    if len(args) != 3 or os.name == "nt":
        return 2
    read_fd, pgid, grace_s = int(args[0]), int(args[1]), float(args[2])
    while _group_exists(pgid):
        readable, _, _ = select.select([read_fd], [], [], 0.25)
        if not readable:
            continue
        command = os.read(read_fd, 1)
        if command == b"D":
            return 0
        if command == b"K":
            _kill_group(pgid, grace_s)
            return 0
        if command == b"":
            _kill_group(pgid, grace_s)
            return 0
    return 0


def _windows_main(args: list[str]) -> int:
    if len(args) != 3 or os.name != "nt":
        return 2
    import ctypes
    from ctypes import wintypes

    parent_pid, target_pid = int(args[0]), int(args[1])
    grace_s = float(args[2])
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    synchronize = 0x00100000
    wait_object_0 = 0
    parent = kernel32.OpenProcess(synchronize, False, parent_pid)
    target = kernel32.OpenProcess(synchronize, False, target_pid)
    try:
        if not target:
            return 0
        while True:
            if kernel32.WaitForSingleObject(target, 0) == wait_object_0:
                return 0
            if not parent or (
                kernel32.WaitForSingleObject(parent, 250) == wait_object_0
            ):
                _windows_kill_tree(target_pid, target, grace_s, kernel32)
                return 0
    finally:
        if parent:
            kernel32.CloseHandle(parent)
        if target:
            kernel32.CloseHandle(target)


def _windows_kill_tree(target_pid: int, target, grace_s: float, kernel32) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(target_pid), "/T"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if kernel32.WaitForSingleObject(target, 50) == 0:
            return
    subprocess.run(
        ["taskkill", "/PID", str(target_pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


if __name__ == "__main__":
    raise SystemExit(main())
