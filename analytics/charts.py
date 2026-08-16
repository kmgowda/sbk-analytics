#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Invoke sbk-charts once with all generated CSVs."""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from .config import OrchestratorConfig
from .policy import SBK_CHARTS_ARTIFACT
from .processes import managed_popen, terminate_process
from .releases import ChartsInstall

log = logging.getLogger(__name__)


def _ai_args(ai_params: dict[str, Any]) -> list[str]:
    """Render AI plugin params as CLI flags (--key value, or --flag for bools)."""
    out: list[str] = []
    for k, v in (ai_params or {}).items():
        key = str(k)
        if not key.startswith("-"):
            key = "--" + key.lstrip("-")
        if isinstance(v, bool):
            if v:
                out.append(key)
        else:
            out.extend([key, str(v)])
    return out


def _prepare_cwd(work_dir: Path) -> Path:
    """sbk-charts reads ``./src/main/banner.txt`` at startup using a
    relative path, but the file is not included in the installed wheel. We
    work around this by running sbk-charts in a dedicated cwd containing a
    stub banner file.
    """
    cwd = work_dir / f"{SBK_CHARTS_ARTIFACT.cache_namespace}-cwd"
    (cwd / "src" / "main").mkdir(parents=True, exist_ok=True)
    banner = cwd / "src" / "main" / "banner.txt"
    if not banner.exists():
        banner.write_text(f"{SBK_CHARTS_ARTIFACT.display_name}\n")
    return cwd


def run_sbk_charts(
    install: ChartsInstall,
    cfg: OrchestratorConfig,
    csv_paths: list[Path],
    output_xlsx: Path,
    *,
    work_dir: Path,
    output_to_stderr: bool = False,
) -> int:
    """Run sbk-charts once. Returns the process exit code."""
    if not csv_paths:
        raise ValueError("no CSV files to feed into sbk-charts")

    cmd: list[str] = [
        str(install.cli),
        "-i",
        ",".join(str(p) for p in csv_paths),
        "-o",
        str(output_xlsx),
    ]
    if cfg.chat:
        cmd.append("-chat")
    # AI backend sub-command is positional
    cmd.append(cfg.ai_model)
    cmd.extend(_ai_args(cfg.ai_params))

    cwd = _prepare_cwd(work_dir)

    banner = [
        "",
        "=" * 78,
        "  LAUNCHING SBK-CHARTS (single invocation, end of run)",
        "=" * 78,
        f"  executable : {cmd[0]}",
        f"  command    : {' '.join(cmd)}",
        f"  cwd        : {cwd}",
        f"  output     : {output_xlsx}",
        f"  ai_model   : {cfg.ai_model}",
        f"  chat mode  : {cfg.chat}",
        f"  -- input CSV files ({len(csv_paths)}) --",
    ]
    for p in csv_paths:
        banner.append(f"    {p}")
    if cfg.ai_params:
        banner.append("  -- AI sub-command params --")
        for k, v in cfg.ai_params.items():
            banner.append(f"    {k}: {v}")
    banner.append("=" * 78)
    # Print banner unconditionally (independent of -v / log level); these are
    # status messages, not debug logs.
    print("\n".join(banner), file=sys.stderr, flush=True)

    proc = managed_popen(
        cmd, cwd=str(cwd),
        stdout=sys.stderr if output_to_stderr else None,
        stderr=sys.stderr if output_to_stderr else None,
    )
    try:
        return proc.wait()
    except BaseException:
        terminate_process(proc)
        raise
