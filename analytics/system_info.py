"""Collect host system info and append a 'system' sheet to the sbk-charts xlsx."""
from __future__ import annotations

import logging
import platform
import shutil
import socket
import subprocess
from datetime import datetime
from pathlib import Path

import psutil
from openpyxl import load_workbook

log = logging.getLogger(__name__)


def _cpu_brand() -> str:
    """Best-effort CPU brand/model string across platforms."""
    sys = platform.system()
    try:
        if sys == "Linux":
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.lower().startswith("model name"):
                            return line.split(":", 1)[1].strip()
            except OSError:
                pass
            if shutil.which("lscpu"):
                out = subprocess.run(
                    ["lscpu"], capture_output=True, text=True, timeout=5
                ).stdout
                for line in out.splitlines():
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif sys == "Darwin":
            out = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if out:
                return out
        elif sys == "Windows":
            return platform.processor() or "unknown"
    except Exception as e:
        log.debug("cpu brand lookup failed: %s", e)
    return platform.processor() or "unknown"


def collect_system_info() -> list[tuple[str, str]]:
    vm = psutil.virtual_memory()
    freq = None
    try:
        f = psutil.cpu_freq()
        if f and f.max:
            freq = f"{f.max:.0f} MHz"
    except Exception:
        pass

    rows: list[tuple[str, str]] = [
        ("Collected at", datetime.now().isoformat(timespec="seconds")),
        ("Hostname", socket.gethostname()),
        ("OS", f"{platform.system()} {platform.release()}"),
        ("OS version", platform.version()),
        ("Architecture", platform.machine()),
        ("Python", platform.python_version()),
        ("CPU model", _cpu_brand()),
        ("Logical CPUs", str(psutil.cpu_count(logical=True) or "unknown")),
        ("Physical CPUs", str(psutil.cpu_count(logical=False) or "unknown")),
        ("CPU frequency", freq or "unknown"),
        ("Total RAM (bytes)", str(vm.total)),
        ("Total RAM (GiB)", f"{vm.total / (1024 ** 3):.2f}"),
        ("Available RAM (GiB)", f"{vm.available / (1024 ** 3):.2f}"),
    ]
    # Disks (best effort)
    try:
        parts = psutil.disk_partitions(all=False)
        for i, p in enumerate(parts):
            try:
                usage = psutil.disk_usage(p.mountpoint)
                rows.append(
                    (
                        f"Disk[{i}] {p.device}",
                        f"mount={p.mountpoint} fstype={p.fstype} "
                        f"total={usage.total / (1024 ** 3):.1f}GiB",
                    )
                )
            except OSError:
                continue
    except Exception as e:
        log.debug("disk enumeration failed: %s", e)
    return rows


def append_system_sheet(xlsx_path: Path, sheet_name: str = "system") -> None:
    """Open xlsx_path and append/replace a sheet with system info."""
    if not xlsx_path.exists():
        raise FileNotFoundError(f"xlsx not found: {xlsx_path}")

    wb = load_workbook(xlsx_path)
    if sheet_name in wb.sheetnames:
        del wb[sheet_name]
    ws = wb.create_sheet(sheet_name)
    ws.append(["Property", "Value"])
    for key, value in collect_system_info():
        ws.append([key, value])
    # widen columns a bit
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 64
    wb.save(xlsx_path)
    log.info("appended '%s' sheet to %s", sheet_name, xlsx_path)
