#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""sbk-analytics command-line entry point."""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__
from .charts import run_sbk_charts
from .config import load_config
from .properties import parse_properties
from .releases import ensure_jdk, ensure_sbk, ensure_sbk_charts
from .runner import _read_yml, run_jobs
from .system_info import append_system_sheet
from .yaml_gen import generate_instance_yaml

log = logging.getLogger("sbk-analytics")


def _print_banner() -> None:
    """Print the sbk-analytics ASCII art banner to stderr."""
    banner_path = Path(__file__).parent / "banner.txt"
    try:
        banner = banner_path.read_text(encoding="utf-8")
        print(banner.format(version=__version__), file=sys.stderr, flush=True)
    except Exception:
        # Fallback if banner file is missing
        print(f"sbk-analytics v{__version__}", file=sys.stderr, flush=True)


def _parse_nodes(value) -> list[str]:
    """Accept a list or a comma/whitespace-separated string of nodes."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(n).strip() for n in value if str(n).strip()]
    s = str(value)
    for sep in (",", "\n", "\t"):
        s = s.replace(sep, " ")
    return [n for n in s.split() if n]


def _build_system_sources(succeeded) -> list[dict]:
    """Build the deduplicated list of system data sources from successful
    SBK job results.

    - All `sbk-yal` instances collapse to a single ``{"kind": "local"}``
      source whose ``instances`` list aggregates their names.
    - Each distinct `sbk-gem-yal` node becomes one ``{"kind": "remote", ...}``
      source whose ``instances`` list aggregates names of gem-yal instances
      that ran on it. SSH credentials are taken from the YAML's
      ``gemuser`` / ``gempass`` / ``gemport`` parameters.
    """
    local_instances: list[str] = []
    remote_map: dict[tuple[str, str, int], dict] = {}

    for r in succeeded:
        params, is_gem = _read_yml(r.yml_path)
        if not is_gem:
            local_instances.append(r.class_name)
            continue
        nodes = _parse_nodes(params.get("nodes"))
        user = str(params.get("gemuser", "")).strip()
        password = str(params.get("gempass", "")).strip()
        try:
            port = int(params.get("gemport", 22))
        except (TypeError, ValueError):
            port = 22
        for node in nodes:
            key = (node, user, port)
            entry = remote_map.setdefault(key, {
                "kind": "remote",
                "node": node,
                "user": user,
                "password": password,
                "port": port,
                "instances": [],
            })
            entry["instances"].append(r.class_name)

    sources: list[dict] = []
    if local_instances:
        sources.append({"kind": "local", "instances": local_instances})
    sources.extend(remote_map.values())
    return sources


def _bundled_versions_file() -> Path:
    """Return the project-bundled `sbk-config.env` shipped next to this package.

    The file lives at the repository root (`<project>/sbk-config.env`).
    """
    return Path(__file__).resolve().parent.parent / "sbk-config.env"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sbk-analytics",
        description=(
            "Orchestrate multiple SBK (sbk-yal / sbk-gem-yal) benchmark runs and "
            "feed the resulting CSVs into sbk-charts for combined Excel + AI analytics."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"sbk-analytics {__version__}",
    )
    p.add_argument(
        "-p",
        "--properties",
        type=Path,
        default=None,
        help=(
            "SBK configuration file (key=value): sbk.version, "
            "sbk-charts.version, sbk.folder, etc. Defaults to the bundled "
            "<project>/sbk-config.env shipped with sbk-analytics."
        ),
    )
    p.add_argument(
        "-c",
        "--config",
        required=True,
        type=Path,
        help="Input YML with the 8 parameter groups (mode, sbk, classes, ...)",
    )
    p.add_argument(
        "-w",
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Working directory for generated YAMLs, CSV files, per-class logs "
            "and the final Excel report. Precedence: this flag > the input "
            "YAML's `workdir:` key > /tmp/sbk-analytics."
        ),
    )
    p.add_argument(
        "-v", "--verbose", action="count", default=0, help="Increase log verbosity."
    )
    p.add_argument(
        "--forward-logs",
        action="store_true",
        help="Force real-time log forwarding (useful on some macOS terminals).",
    )
    return p.parse_args(argv)


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING - 10 * verbosity
    level = max(logging.DEBUG, level)
    
    # Force reconfiguration to ensure it works on all platforms
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    
    # Ensure all handlers flush immediately (especially important for macOS)
    for handler in logging.root.handlers:
        if hasattr(handler, 'stream'):
            handler.stream.flush()
    
    # Log configuration for debugging
    log.debug("Logging configured at level %s", logging.getLevelName(level))
    log.debug("Python version: %s", sys.version)
    log.debug("Platform: %s", sys.platform)



def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _print_banner()
    _setup_logging(args.verbose)

    properties_path = args.properties or _bundled_versions_file()
    if not properties_path.is_file():
        raise SystemExit(
            f"versions properties file not found: {properties_path}\n"
            f"either pass -p / --properties or restore the bundled "
            f"{_bundled_versions_file()}"
        )
    versions = parse_properties(properties_path)
    log.info("using properties file: %s", properties_path)
    cfg = load_config(args.config)

    log.info("SBK: %s @ %s", versions.sbk_url, versions.sbk)
    log.info("sbk-charts: %s @ %s", versions.sbk_charts_url, versions.sbk_charts)
    log.info(
        "mode=%s instances=%d uses_gem=%s",
        cfg.mode,
        len(cfg.instances),
        cfg.uses_gem,
    )
    for inst in cfg.instances:
        log.info("  - %s (class=%s) params=%s", inst.name, inst.class_name, inst.params)

    # Working directory: precedence is
    #   1. -w / --work-dir CLI flag
    #   2. workdir: in the input YAML (just after `mode:`)
    #   3. /tmp/sbk-analytics  (DEFAULT_WORKDIR)
    work = args.work_dir if args.work_dir is not None else Path(cfg.workdir)
    work.mkdir(parents=True, exist_ok=True)
    log.info("work dir: %s", work.resolve())

    # 1. Resolve the required JDK (used via SBK_JAVA_HOME), SBK, and sbk-charts.
    #    ensure_jdk() first checks the existing SBK_JAVA_HOME / JAVA_HOME /
    #    `java` on PATH for a matching major version; only downloads if none
    #    match. The user pins the required major version in sbk-config.env via
    #    `sbk.jdk.version=...` (default 25).
    print("\n=== Resolving dependencies ===", flush=True)
    print("This may take a while on first run (downloads and caches JDK, SBK, sbk-charts)...", flush=True)
    print("Download progress will be shown in the logs below...", flush=True)
    
    jdk = ensure_jdk(versions.sbk_jdk, jdk_folder=versions.jdk_folder, ssl_verify=versions.ssl_verify)
    log.info("JDK %s home: %s", versions.sbk_jdk, jdk.home)
    print(f"✓ JDK {versions.sbk_jdk} ready", flush=True)
    
    sbk = ensure_sbk(versions.sbk, repo=versions.sbk_repo, sbk_folder=versions.sbk_folder, ssl_verify=versions.ssl_verify)
    print(f"✓ SBK {versions.sbk} ready", flush=True)
    
    charts = ensure_sbk_charts(versions.sbk_charts, repo_url=versions.sbk_charts_url, sbk_folder=versions.sbk_folder, ssl_verify=versions.ssl_verify)
    print(f"✓ sbk-charts {versions.sbk_charts} ready", flush=True)

    executable = sbk.sbk_gem_yal if cfg.uses_gem else sbk.sbk_yal
    log.info("using SBK executable: %s", executable)

    # 2. Generate per-class YAMLs
    yml_dir = work / "yml"
    csv_dir = work / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, Path, Path]] = []
    for inst in cfg.instances:
        csv_path = (csv_dir / f"sbk-{inst.name}.csv").resolve()
        yml_path = generate_instance_yaml(inst, yml_dir, csv_path)
        jobs.append((inst.name, yml_path, csv_path))

    # 3. Run SBK instances
    log_dir = work / "logs"
    results = run_jobs(
        executable, jobs, mode=cfg.mode, log_dir=log_dir, jdk_home=jdk.home,
        forward_logs=args.forward_logs,
    )

    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print("\n=== SBK run summary ===", flush=True)
    for r in results:
        status = "OK" if r.ok else f"FAIL(rc={r.returncode})"
        extra = f" log={r.log_path}" if r.log_path else ""
        print(f"  {status:14s} instance={r.class_name} csv={r.csv_path}{extra}")

    # Resolve sbk-charts.use_files: take each entry as a CSV path, optionally
    # relative to the working directory. Drop (with a warning) any that don't
    # exist; sbk-charts would fail anyway.
    extra_csvs: list[Path] = []
    for raw_path in cfg.use_files:
        p = Path(raw_path)
        if not p.is_absolute():
            p = (work / p).resolve()
        else:
            p = p.resolve()
        if p.is_file() and p.stat().st_size > 0:
            extra_csvs.append(p)
        else:
            print(
                f"WARNING: sbk-charts.use_files entry ignored (missing or "
                f"empty): {p}",
                file=sys.stderr, flush=True,
            )
    if extra_csvs:
        print("\n=== sbk-charts use_files (pre-existing) ===", flush=True)
        for p in extra_csvs:
            print(f"  USE            csv={p}")

    if not succeeded and not extra_csvs:
        print(
            "All SBK instances failed and no use_files supplied; skipping "
            "sbk-charts as per spec.",
            file=sys.stderr,
            flush=True,
        )
        return 2

    if failed:
        print(
            f"WARNING: {len(failed)} of {len(results)} SBK runs failed; "
            f"continuing with {len(succeeded)} fresh CSV(s) and "
            f"{len(extra_csvs)} pre-existing use_files.",
            file=sys.stderr,
            flush=True,
        )

    # 4. sbk-charts (once)
    # The output xlsx lives in <workdir> unless cfg.output is an absolute path
    # (or contains an explicit directory component) -- in which case we honour
    # the user's exact location.
    output_p = Path(cfg.output)
    if output_p.is_absolute() or len(output_p.parts) > 1:
        output_xlsx = output_p.resolve()
    else:
        output_xlsx = (work / output_p).resolve()
    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    csv_paths = [r.csv_path for r in succeeded] + extra_csvs

    rc = run_sbk_charts(charts, cfg, csv_paths, output_xlsx, work_dir=work)
    if rc != 0:
        log.error("sbk-charts exited with rc=%s", rc)
        return rc
    if not output_xlsx.exists():
        log.error("sbk-charts did not produce expected output: %s", output_xlsx)
        return 3

    # 5. Append system sheet (one row per distinct host: local + remote nodes
    #    visited by sbk-gem-yal instances).
    try:
        sources = _build_system_sources(succeeded)
        append_system_sheet(output_xlsx, sources=sources)
    except Exception as e:
        log.error("failed to append system sheet: %s", e)
        return 4

    print(f"\nDone. Output: {output_xlsx}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
