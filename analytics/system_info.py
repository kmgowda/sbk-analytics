#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Collect system + container info and append a ``system`` sheet to the
sbk-charts xlsx.

The sheet uses a **column-per-attribute, row-per-host** layout. There is
exactly one row for every distinct system that participated in the run:

- One row for the **local** host if any ``sbk-yal`` instance ran (one row
  total, regardless of how many local sbk-yal instances).
- One row per **distinct remote node** if any ``sbk-gem-yal`` instance ran.
  Remote rows are populated by SSHing into each node (using ``gemuser`` /
  ``gempass`` / ``gemport`` from the instance YAML) and running a small
  POSIX shell script that prints key=value pairs.

Each row records OS, CPU, RAM, **Docker / Kubernetes container details**,
and which sbk-analytics instances ran on that system.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

import psutil
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .policy import RUNTIME_POLICY

log = logging.getLogger(__name__)
SYSTEM_INFO_POLICY = RUNTIME_POLICY.system_info
SSH_POLICY = RUNTIME_POLICY.ssh
DISPLAY_POLICY = RUNTIME_POLICY.display


# Columns of the system sheet, in order.
SYSTEM_COLUMNS: list[str] = [
    "Source",
    "Used by instances",
    "Hostname",
    "OS",
    "OS version",
    "Architecture",
    "CPU model",
    "Physical CPUs",
    "Logical CPUs",
    "CPU MHz",
    "Total RAM (GiB)",
    "Available RAM (GiB)",
    "Container runtime",
    "Container ID",
    "K8s Pod",
    "K8s Namespace",
    "Collected at",
    "Status",
]


# ---------------------------------------------------------------------------
# Local system info
# ---------------------------------------------------------------------------


def _cpu_brand() -> str:
    """Best-effort CPU brand/model string across platforms."""
    sysname = platform.system()
    try:
        if sysname == "Linux":
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.lower().startswith("model name"):
                            return line.split(":", 1)[1].strip()
            except OSError:
                pass
            if shutil.which("lscpu"):
                out = subprocess.run(
                    ["lscpu"], capture_output=True, text=True,
                    timeout=SYSTEM_INFO_POLICY.local_command_timeout_s,
                ).stdout
                for line in out.splitlines():
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif sysname == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True, text=True,
                timeout=SYSTEM_INFO_POLICY.local_command_timeout_s,
            ).stdout.strip()
            if out:
                return out
    except Exception as e:
        log.debug("cpu brand lookup failed: %s", e)
    return platform.processor() or "unknown"


def _container_info() -> dict[str, str]:
    """Detect Docker / Kubernetes container details for the current process."""
    runtime = "none"
    container_id = ""
    k8s_pod = ""
    k8s_namespace = ""

    if Path("/.dockerenv").exists():
        runtime = "docker"
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        runtime = "kubernetes"
    try:
        cgroup = Path("/proc/1/cgroup").read_text()
        if "/kubepods" in cgroup or "/kubelet" in cgroup:
            runtime = "kubernetes"
        elif runtime == "none" and ("/docker" in cgroup or "/containerd" in cgroup):
            runtime = "docker"
    except OSError:
        pass

    if runtime != "none":
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                last = line.rstrip().split("/")[-1]
                if last:
                    container_id = last[:64]
                    break
        except OSError:
            pass

    if runtime == "kubernetes":
        k8s_pod = (
            os.environ.get("POD_NAME")
            or os.environ.get("HOSTNAME")
            or socket.gethostname()
        )
        try:
            ns_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
            if ns_path.is_file():
                k8s_namespace = ns_path.read_text().strip()
        except OSError:
            pass

    return {
        "Container runtime": runtime,
        "Container ID": container_id,
        "K8s Pod": k8s_pod,
        "K8s Namespace": k8s_namespace,
    }


def collect_local_system_info() -> dict[str, str]:
    """Return system + container info for the local host."""
    vm = psutil.virtual_memory()
    freq = None
    try:
        f = psutil.cpu_freq()
        if f and f.max:
            freq = f"{f.max:.0f}"
    except Exception:
        pass

    info: dict[str, str] = {
        "Hostname": socket.gethostname(),
        "OS": f"{platform.system()} {platform.release()}",
        "OS version": platform.version(),
        "Architecture": platform.machine(),
        "CPU model": _cpu_brand(),
        "Physical CPUs": str(psutil.cpu_count(logical=False) or ""),
        "Logical CPUs": str(psutil.cpu_count(logical=True) or ""),
        "CPU MHz": freq or "",
        "Total RAM (GiB)": (
            f"{vm.total / (DISPLAY_POLICY.bytes_per_kibibyte ** 3):.2f}"
        ),
        "Available RAM (GiB)": (
            f"{vm.available / (DISPLAY_POLICY.bytes_per_kibibyte ** 3):.2f}"
        ),
    }
    info.update(_container_info())
    info["Collected at"] = datetime.now().isoformat(timespec="seconds")
    info["Status"] = "ok"
    return info


# ---------------------------------------------------------------------------
# Remote system info (over SSH)
# ---------------------------------------------------------------------------


# A small POSIX-shell probe that prints key=value pairs to stdout. Designed
# to run on bare-metal Linux, Docker containers, and Kubernetes pods alike.
_REMOTE_INFO_SCRIPT = r"""
set +e
echo "hostname=$(hostname 2>/dev/null)"
echo "os=$(uname -s 2>/dev/null) $(uname -r 2>/dev/null)"
echo "os_version=$(uname -v 2>/dev/null)"
echo "arch=$(uname -m 2>/dev/null)"
echo "logical_cpus=$(nproc 2>/dev/null)"
echo "physical_cpus=$(lscpu 2>/dev/null | awk -F: '/^Socket\(s\)/{gsub(/ /,"",$2); print $2}')"
echo "cpu_model=$(awk -F: '/^model name/{sub(/^ +/,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null)"
echo "cpu_mhz=$(awk -F: '/^cpu MHz/{gsub(/ /,"",$2); print $2; exit}' /proc/cpuinfo 2>/dev/null)"
mem_total_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null)
mem_avail_kb=$(awk '/^MemAvailable:/{print $2}' /proc/meminfo 2>/dev/null)
echo "total_ram_kb=${mem_total_kb:-}"
echo "avail_ram_kb=${mem_avail_kb:-}"
runtime=none
if [ -f /.dockerenv ]; then runtime=docker; fi
if [ -n "$KUBERNETES_SERVICE_HOST" ]; then runtime=kubernetes; fi
if grep -q "/kubepods\|/kubelet" /proc/1/cgroup 2>/dev/null; then runtime=kubernetes; fi
if [ "$runtime" = "none" ] && grep -q "/docker\|/containerd" /proc/1/cgroup 2>/dev/null; then runtime=docker; fi
echo "container_runtime=$runtime"
container_id=$(awk -F/ '{ for (i=NF; i>=1; i--) if ($i != "") { print $i; exit } }' /proc/self/cgroup 2>/dev/null | head -c 64)
echo "container_id=${container_id}"
echo "k8s_pod=${POD_NAME:-${HOSTNAME:-}}"
ns=""
if [ -r /var/run/secrets/kubernetes.io/serviceaccount/namespace ]; then
  ns=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null)
fi
echo "k8s_namespace=${ns}"
"""


def collect_remote_system_info(
    node: str,
    *,
    user: str = "",
    password: str = "",
    port: int = SSH_POLICY.default_port,
    timeout_s: float = SSH_POLICY.system_info_command_timeout_s,
) -> dict[str, str]:
    """Return system + container info for a remote node, via SSH.

    If SSH or the probe fails, returns a dict with ``Status`` set to a
    short error description; the row will still be added to the sheet with
    whatever info we already know (just the node name in that case).
    """
    target = f"{user}@{node}" if user else node
    ssh_args = [
        "ssh",
        "-p", str(int(port)),
        *SSH_POLICY.host_key_arguments,
        "-o", "BatchMode=" + ("no" if password else "yes"),
        "-o", f"ConnectTimeout={SSH_POLICY.connect_timeout_s}",
        target,
        "bash -s",
    ]
    have_sshpass = bool(password) and shutil.which("sshpass") is not None
    if password and not have_sshpass:
        log.warning(
            "remote system info: 'sshpass' not on PATH but gempass is set for "
            "%s; attempting key-based ssh which may fail", node,
        )
    if have_sshpass:
        env = {**os.environ, "SSHPASS": password}
        cmd = ["sshpass", "-e", *ssh_args]
    else:
        env = os.environ.copy()
        cmd = ssh_args

    try:
        proc = subprocess.run(
            cmd, env=env,
            input=_REMOTE_INFO_SCRIPT,
            capture_output=True, text=True, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return {"Status": f"ssh timeout after {int(timeout_s)}s"}
    except OSError as e:
        return {"Status": f"ssh error: {e}"}

    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {
            "Status": (
                f"ssh rc={proc.returncode}: "
                f"{err[0][:DISPLAY_POLICY.system_info_tail_characters]}"
            )
        }

    raw: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.rstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        raw[k.strip()] = v.strip()

    def _bytes_to_gib(kb_str: str) -> str:
        try:
            return (
                f"{int(kb_str) / (DISPLAY_POLICY.bytes_per_kibibyte ** 2):.2f}"
            )
        except (TypeError, ValueError):
            return ""

    info: dict[str, str] = {
        "Hostname": raw.get("hostname", ""),
        "OS": raw.get("os", ""),
        "OS version": raw.get("os_version", ""),
        "Architecture": raw.get("arch", ""),
        "CPU model": raw.get("cpu_model", ""),
        "Physical CPUs": raw.get("physical_cpus", ""),
        "Logical CPUs": raw.get("logical_cpus", ""),
        "CPU MHz": raw.get("cpu_mhz", ""),
        "Total RAM (GiB)": _bytes_to_gib(raw.get("total_ram_kb", "")),
        "Available RAM (GiB)": _bytes_to_gib(raw.get("avail_ram_kb", "")),
        "Container runtime": raw.get("container_runtime", "none"),
        "Container ID": raw.get("container_id", ""),
        "K8s Pod": raw.get("k8s_pod", ""),
        "K8s Namespace": raw.get("k8s_namespace", ""),
        "Collected at": datetime.now().isoformat(timespec="seconds"),
        "Status": "ok",
    }
    return info


# ---------------------------------------------------------------------------
# Sheet writer
# ---------------------------------------------------------------------------


def append_system_sheet(
    xlsx_path: Path,
    *,
    sources: Iterable[dict],
    sheet_name: str = SYSTEM_INFO_POLICY.default_sheet_name,
) -> None:
    """Append (or replace) the ``system`` sheet on the xlsx with one row per
    distinct system.

    Each entry in ``sources`` describes one system::

        {"kind": "local", "instances": ["file", "rocksdb"]}
        {"kind": "remote", "node": "n1", "user": "kmg", "password": "pw",
         "port": 22, "instances": ["kafka"]}

    A single fallback row (``Source="local"``, no instances) is written if
    ``sources`` is empty -- that way the sheet always has at least one data
    row describing the host that produced the report.
    """
    xlsx_path = Path(xlsx_path)
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")

    sources = list(sources)
    if not sources:
        sources = [{"kind": "local", "instances": []}]

    wb = load_workbook(xlsx_path)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(SYSTEM_COLUMNS)

    for src in sources:
        kind = src.get("kind", "local")
        instances = ", ".join(src.get("instances") or [])
        if kind == "remote":
            source_label = f"remote: {src['node']}"
            info = collect_remote_system_info(
                src["node"],
                user=src.get("user", ""),
                password=src.get("password", ""),
                port=int(src.get("port", 22)),
            )
        else:
            source_label = "local"
            info = collect_local_system_info()

        row = [source_label, instances] + [
            info.get(col, "") for col in SYSTEM_COLUMNS[2:]
        ]
        ws.append(row)

    # Column widths
    widths = {
        "Source": 22,
        "Used by instances": 26,
        "Hostname": 24,
        "OS": 24,
        "OS version": 32,
        "Architecture": 14,
        "CPU model": 38,
        "Physical CPUs": 14,
        "Logical CPUs": 14,
        "CPU MHz": 12,
        "Total RAM (GiB)": 18,
        "Available RAM (GiB)": 20,
        "Container runtime": 18,
        "Container ID": 34,
        "K8s Pod": 28,
        "K8s Namespace": 18,
        "Collected at": 22,
        "Status": 22,
    }
    for idx, col in enumerate(SYSTEM_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(col, 16)

    # Freeze the header row.
    ws.freeze_panes = "A2"

    wb.save(xlsx_path)
    log.info(
        "appended '%s' sheet to %s (%d row%s)",
        sheet_name, xlsx_path, len(sources), "" if len(sources) == 1 else "s",
    )
