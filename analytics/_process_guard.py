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
import sys
import time
from pathlib import Path

from .policy import RUNTIME_POLICY

PROCESS_POLICY = RUNTIME_POLICY.processes


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
        time.sleep(PROCESS_POLICY.guard_poll_interval_s)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + PROCESS_POLICY.guard_exit_padding_s
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return
        time.sleep(PROCESS_POLICY.guard_poll_interval_s)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (3, 4):
        return 2
    read_fd, pgid, grace_s = int(args[0]), int(args[1]), float(args[2])
    record = Path(args[3]) if len(args) == 4 else None
    try:
        while _group_exists(pgid):
            readable, _, _ = select.select(
                [read_fd], [], [], PROCESS_POLICY.guard_pipe_poll_interval_s
            )
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
    finally:
        # Preserve the durable record if the group could not be removed. A
        # later invocation will quarantine it instead of risking PID reuse.
        if record is not None and not _group_exists(pgid):
            record.unlink(missing_ok=True)

if __name__ == "__main__":
    raise SystemExit(main())
