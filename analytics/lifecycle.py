#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Durable ownership records for locally managed workload process groups.

The in-memory registry in :mod:`analytics.processes` handles the current
invocation.  This module adds the cross-invocation half of that contract: each
workload is recorded with PID creation times and its process group, and a later
invocation safely reconciles records whose controller no longer exists.

Records deliberately exclude credentials.  GEM records may contain node names
for diagnostics, but remote cleanup remains SBK-GEM's responsibility because
SBK does not expose a run-scoped remote cleanup command to this application.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Sequence

import psutil

from .policy import APPLICATION, RUNTIME_POLICY

log = logging.getLogger(__name__)
ENVIRONMENT_POLICY = RUNTIME_POLICY.environment
LIFECYCLE_POLICY = RUNTIME_POLICY.lifecycle
PROCESS_POLICY = RUNTIME_POLICY.processes

_RUN_ID = uuid.uuid4().hex


def current_run_id() -> str:
    """Return the immutable identifier for this analytics invocation."""
    return _RUN_ID


def registry_root() -> Path:
    """Return the platform state directory without creating it."""
    configured = os.environ.get(ENVIRONMENT_POLICY.lifecycle_folder)
    if configured:
        return Path(configured).expanduser()
    application_state = os.environ.get(
        ENVIRONMENT_POLICY.application_state_home
    )
    if application_state:
        return (
            Path(application_state).expanduser()
            / LIFECYCLE_POLICY.registry_directory
        )
    xdg = os.environ.get(ENVIRONMENT_POLICY.xdg_state_home)
    if xdg:
        state = Path(xdg).expanduser()
    elif sys.platform == "darwin":
        state = Path.home().joinpath(*LIFECYCLE_POLICY.macos_state_path)
    else:
        state = Path.home().joinpath(*LIFECYCLE_POLICY.linux_state_path)
    return state / APPLICATION.name / LIFECYCLE_POLICY.registry_directory


def record_path(pid: int) -> Path:
    return registry_root() / f"{_RUN_ID}-{pid}{LIFECYCLE_POLICY.record_suffix}"


def _process_create_time(pid: int) -> float:
    return psutil.Process(pid).create_time()


def _command_list(args: Any) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    return [os.fsdecode(value) for value in args]


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
        mode=LIFECYCLE_POLICY.registry_directory_mode,
    )
    path.parent.chmod(LIFECYCLE_POLICY.registry_directory_mode)
    temporary = path.with_name(
        f"{path.name}-{os.getpid()}{LIFECYCLE_POLICY.temporary_suffix}"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(LIFECYCLE_POLICY.record_mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def register_process(
    pid: int,
    args: Any,
    *,
    role: str,
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> Path:
    """Persist ownership after both workload and parent-death guard start."""
    target = path or record_path(pid)
    command = _command_list(args)
    payload = {
        LIFECYCLE_POLICY.schema_field: LIFECYCLE_POLICY.schema_version,
        LIFECYCLE_POLICY.run_id_field: _RUN_ID,
        LIFECYCLE_POLICY.controller_pid_field: os.getpid(),
        LIFECYCLE_POLICY.controller_create_time_field:
            _process_create_time(os.getpid()),
        LIFECYCLE_POLICY.process_pid_field: pid,
        LIFECYCLE_POLICY.process_create_time_field: _process_create_time(pid),
        LIFECYCLE_POLICY.process_group_field: os.getpgid(pid),
        LIFECYCLE_POLICY.role_field: role,
        LIFECYCLE_POLICY.command_field: command,
        LIFECYCLE_POLICY.metadata_field: metadata or {},
        LIFECYCLE_POLICY.created_at_field: time.time(),
    }
    _atomic_write(target, payload)
    return target


def unregister_process(path: Path | None) -> None:
    if path is not None:
        path.unlink(missing_ok=True)


def _read_record(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("lifecycle record is not an object")
    if (
        value.get(LIFECYCLE_POLICY.schema_field)
        != LIFECYCLE_POLICY.schema_version
    ):
        raise ValueError("unsupported lifecycle record schema")
    return value


def _identity_matches(
    pid: int,
    created: float,
    *,
    pgid: int | None = None,
    command: Sequence[str] | None = None,
    run_id: str | None = None,
) -> bool:
    try:
        process = psutil.Process(pid)
        if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return False
        if abs(process.create_time() - created) > LIFECYCLE_POLICY.identity_tolerance_s:
            return False
        if pgid is not None and os.getpgid(pid) != pgid:
            return False
        if run_id is not None:
            try:
                if (
                    process.environ().get(
                        ENVIRONMENT_POLICY.lifecycle_run_id
                    ) == run_id
                ):
                    return True
            except (psutil.Error, OSError) as exc:
                # Same-user environment inspection may still be restricted on
                # hardened hosts; retain the recorded command fallback.
                log.debug(
                    "could not read lifecycle run ID for pid=%s; using "
                    "recorded command identity: %s",
                    pid,
                    exc,
                )
        if command:
            expected = Path(command[0]).name
            actual = process.cmdline()
            if expected and not any(Path(token).name == expected for token in actual):
                return False
        return True
    except (psutil.Error, OSError, ValueError):
        return False


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A process group containing only unreaped zombies owns no executable
    # workload and cannot be affected by another signal. Treat it as gone so
    # records do not remain unresolved solely because an init process has not
    # reaped a terminated descendant yet.
    for process in psutil.process_iter((
        LIFECYCLE_POLICY.process_id_attribute,
        LIFECYCLE_POLICY.process_status_attribute,
    )):
        try:
            if (
                os.getpgid(process.pid) == pgid
                and process.info[LIFECYCLE_POLICY.process_status_attribute]
                != psutil.STATUS_ZOMBIE
            ):
                return True
        except (psutil.NoSuchProcess, ProcessLookupError):
            continue
        except (psutil.AccessDenied, PermissionError):
            return True
    return False


def _group_run_identity_matches(pgid: int, run_id: str) -> bool:
    """Confirm every visible member of a leaderless group belongs to one run."""
    matched = False
    for process in psutil.process_iter((
        LIFECYCLE_POLICY.process_id_attribute,
        LIFECYCLE_POLICY.process_status_attribute,
    )):
        try:
            if (
                os.getpgid(process.pid) != pgid
                or process.info[LIFECYCLE_POLICY.process_status_attribute]
                == psutil.STATUS_ZOMBIE
            ):
                continue
            matched = True
            if (
                process.environ().get(ENVIRONMENT_POLICY.lifecycle_run_id)
                != run_id
            ):
                return False
        except (psutil.Error, OSError) as exc:
            # Ambiguous ownership must never result in a signal.
            log.debug(
                "could not verify lifecycle run ID for process-group "
                "member pid=%s pgid=%s; leaving the group untouched: %s",
                process.pid,
                pgid,
                exc,
            )
            return False
    return matched


def _terminate_group(pgid: int) -> bool:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + PROCESS_POLICY.termination_grace_s
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return True
        time.sleep(LIFECYCLE_POLICY.reconciliation_poll_interval_s)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + PROCESS_POLICY.guard_exit_padding_s
    while time.monotonic() < deadline:
        if not _group_exists(pgid):
            return True
        time.sleep(LIFECYCLE_POLICY.reconciliation_poll_interval_s)
    return not _group_exists(pgid)


def _quarantine(path: Path, reason: str) -> None:
    destination = path.with_suffix(
        path.suffix + LIFECYCLE_POLICY.unresolved_suffix
    )
    log.error("lifecycle ownership could not be verified for %s: %s", path, reason)
    try:
        path.replace(destination)
    except OSError:
        pass


def inspect_records() -> dict[str, Any]:
    """Return a read-only lifecycle summary for dependency diagnostics."""
    root = registry_root()
    records: list[dict[str, Any]] = []
    if root.is_dir():
        for path in sorted(root.glob(f"*{LIFECYCLE_POLICY.record_suffix}")):
            try:
                record = _read_record(path)
                controller_active = _identity_matches(
                    int(record[LIFECYCLE_POLICY.controller_pid_field]),
                    float(record[LIFECYCLE_POLICY.controller_create_time_field]),
                )
                process_active = _identity_matches(
                    int(record[LIFECYCLE_POLICY.process_pid_field]),
                    float(record[LIFECYCLE_POLICY.process_create_time_field]),
                    pgid=int(record[LIFECYCLE_POLICY.process_group_field]),
                    command=record.get(LIFECYCLE_POLICY.command_field),
                    run_id=record.get(LIFECYCLE_POLICY.run_id_field),
                )
                group_active = _group_exists(
                    int(record[LIFECYCLE_POLICY.process_group_field])
                )
                records.append({
                    LIFECYCLE_POLICY.run_id_field:
                        record.get(LIFECYCLE_POLICY.run_id_field),
                    LIFECYCLE_POLICY.role_field:
                        record.get(LIFECYCLE_POLICY.role_field),
                    LIFECYCLE_POLICY.process_pid_field:
                        record.get(LIFECYCLE_POLICY.process_pid_field),
                    LIFECYCLE_POLICY.process_group_field:
                        record.get(LIFECYCLE_POLICY.process_group_field),
                    LIFECYCLE_POLICY.controller_pid_field:
                        record.get(LIFECYCLE_POLICY.controller_pid_field),
                    LIFECYCLE_POLICY.controller_active_field: controller_active,
                    LIFECYCLE_POLICY.process_active_field: process_active,
                    LIFECYCLE_POLICY.group_active_field: group_active,
                    LIFECYCLE_POLICY.metadata_field:
                        record.get(LIFECYCLE_POLICY.metadata_field) or {},
                })
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                records.append({
                    LIFECYCLE_POLICY.record_field: str(path),
                    LIFECYCLE_POLICY.error_field: str(exc),
                })
    unresolved = (
        len(list(root.glob(f"*{LIFECYCLE_POLICY.unresolved_suffix}")))
        if root.is_dir() else 0
    )
    return {
        LIFECYCLE_POLICY.registry_field: str(root),
        LIFECYCLE_POLICY.records_field: records,
        LIFECYCLE_POLICY.active_field: sum(
            1 for item in records
            if item.get(LIFECYCLE_POLICY.controller_active_field)
        ),
        LIFECYCLE_POLICY.stale_field: sum(
            1 for item in records
            if not item.get(LIFECYCLE_POLICY.controller_active_field)
            and (
                item.get(LIFECYCLE_POLICY.process_active_field)
                or item.get(LIFECYCLE_POLICY.group_active_field)
            )
        ),
        LIFECYCLE_POLICY.unresolved_field: unresolved,
    }


def reconcile_stale_records() -> dict[str, int]:
    """Clean only verified workloads whose recorded controller has vanished."""
    root = registry_root()
    summary = {
        LIFECYCLE_POLICY.active_field: 0,
        LIFECYCLE_POLICY.cleaned_field: 0,
        LIFECYCLE_POLICY.expired_field: 0,
        LIFECYCLE_POLICY.unresolved_field: 0,
    }
    if not root.is_dir():
        return summary
    for path in sorted(root.glob(f"*{LIFECYCLE_POLICY.record_suffix}")):
        try:
            record = _read_record(path)
            if _identity_matches(
                int(record[LIFECYCLE_POLICY.controller_pid_field]),
                float(record[LIFECYCLE_POLICY.controller_create_time_field]),
            ):
                summary[LIFECYCLE_POLICY.active_field] += 1
                continue
            pid = int(record[LIFECYCLE_POLICY.process_pid_field])
            pgid = int(record[LIFECYCLE_POLICY.process_group_field])
            if not psutil.pid_exists(pid):
                if _group_exists(pgid):
                    run_id = str(
                        record.get(LIFECYCLE_POLICY.run_id_field) or ""
                    )
                    if run_id and _group_run_identity_matches(pgid, run_id):
                        log.warning(
                            "reconciling leaderless stale %s workload "
                            "run=%s pgid=%s",
                            record.get(LIFECYCLE_POLICY.role_field), run_id, pgid,
                        )
                        if _terminate_group(pgid):
                            path.unlink(missing_ok=True)
                            summary[LIFECYCLE_POLICY.cleaned_field] += 1
                        else:
                            _quarantine(path, "verified process group survived SIGKILL")
                            summary[LIFECYCLE_POLICY.unresolved_field] += 1
                    else:
                        _quarantine(
                            path,
                            "recorded leader exited and remaining group ownership "
                            "could not be verified",
                        )
                        summary[LIFECYCLE_POLICY.unresolved_field] += 1
                else:
                    path.unlink(missing_ok=True)
                    summary[LIFECYCLE_POLICY.expired_field] += 1
                continue
            if not _identity_matches(
                pid,
                float(record[LIFECYCLE_POLICY.process_create_time_field]),
                pgid=pgid,
                command=record.get(LIFECYCLE_POLICY.command_field),
                run_id=record.get(LIFECYCLE_POLICY.run_id_field),
            ):
                _quarantine(path, "PID, process-group, or command identity changed")
                summary[LIFECYCLE_POLICY.unresolved_field] += 1
                continue
            log.warning(
                "reconciling stale %s workload run=%s pid=%s pgid=%s",
                record.get(LIFECYCLE_POLICY.role_field),
                record.get(LIFECYCLE_POLICY.run_id_field), pid, pgid,
            )
            if _terminate_group(pgid):
                path.unlink(missing_ok=True)
                summary[LIFECYCLE_POLICY.cleaned_field] += 1
            else:
                _quarantine(path, "verified process group survived SIGKILL")
                summary[LIFECYCLE_POLICY.unresolved_field] += 1
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            _quarantine(path, str(exc))
            summary[LIFECYCLE_POLICY.unresolved_field] += 1
    return summary
