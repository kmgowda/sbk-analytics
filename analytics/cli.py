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
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

from . import __version__
from .charts import run_sbk_charts
from .config import load_config
from .errors import ConfigurationError, DependencyResolutionError, SbkAnalyticsError
from .policy import APPLICATION, RUNTIME_POLICY, SBK_ARTIFACT, SBK_CHARTS_ARTIFACT
from .properties import parse_properties
from .processes import ProcessExit, child_process_cleanup
from .releases import (
    ChartsInstall,
    DependencySource,
    SbkInstall,
    cache_root,
    ensure_jdk,
    ensure_sbk,
    ensure_sbk_charts,
    inspect_shared_sbk,
    inspect_shared_sbk_charts,
    managed_metadata,
)
from .runner import _read_yml, run_jobs
from .system_info import append_system_sheet
from .yaml_gen import generate_instance_yaml

log = logging.getLogger(APPLICATION.name)
CACHE_POLICY = RUNTIME_POLICY.cache
EXIT_CODES = RUNTIME_POLICY.exit_codes
PROVENANCE_POLICY = RUNTIME_POLICY.provenance
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface
DISPLAY_POLICY = RUNTIME_POLICY.display
SSH_POLICY = RUNTIME_POLICY.ssh


def _print_sbk_resolution(install: SbkInstall, version: str) -> None:
    """Print the selected SBK source even when verbose logging is disabled."""
    print(f"[ok] SBK source       : {install.source.value}", flush=True)
    print(f"  folder           : {install.home}", flush=True)
    print(f"  sbk-yal          : {install.sbk_yal}", flush=True)
    gem_executable = install.sbk_gem_yal or "not available (not required)"
    print(f"  sbk-gem-yal      : {gem_executable}", flush=True)
    _print_source_provenance(
        install.provenance,
        local_action=PROVENANCE_POLICY.sbk_local_action,
    )
    if install.source is DependencySource.LOCAL:
        print(f"  detected version : {install.detected_version or 'unknown'}", flush=True)
        print(f"  configured version: {version} (policy applies)", flush=True)
    else:
        print(f"  version          : {version}", flush=True)


def _print_charts_resolution(install: ChartsInstall, version: str) -> None:
    """Print the selected sbk-charts source and exact executable."""
    print(f"[ok] sbk-charts source: {install.source.value}", flush=True)
    print(f"  folder           : {install.venv_dir}", flush=True)
    print(f"  executable       : {install.cli}", flush=True)
    _print_source_provenance(
        install.provenance,
        local_action=PROVENANCE_POLICY.charts_local_action,
    )
    if install.source is DependencySource.LOCAL:
        print(
            f"  detected version : {install.detected_version or 'unknown'}",
            flush=True,
        )
        print(f"  configured version: {version} (policy applies)", flush=True)
    elif install.source is DependencySource.CONDA:
        print(
            f"  configured version: {version}; detected: "
            f"{install.detected_version or 'unknown'}",
            flush=True,
        )
    else:
        print(f"  version          : {version}", flush=True)


def _print_source_provenance(provenance, *, local_action: str) -> None:
    """Print consistent release/shared-folder origin details."""
    if provenance is None:
        return
    selection = (
        PROVENANCE_POLICY.shared_folder_display
        if provenance.mode == PROVENANCE_POLICY.shared_folder_mode
        else PROVENANCE_POLICY.github_release_display
    )
    print(f"  selection        : {selection}", flush=True)
    print(f"  layout           : {provenance.layout}", flush=True)
    if provenance.configured_location:
        print(
            f"  configured path  : {provenance.configured_location}",
            flush=True,
        )
    if provenance.resolved_location:
        print(f"  resolved path    : {provenance.resolved_location}", flush=True)
    if provenance.repository_url:
        print(f"  repository       : {provenance.repository_url}", flush=True)
    if provenance.release_tag:
        print(f"  release tag      : {provenance.release_tag}", flush=True)
    if provenance.asset:
        print(f"  release asset    : {provenance.asset}", flush=True)
    if provenance.sha256:
        print(f"  SHA-256          : {provenance.sha256}", flush=True)
    if provenance.revision:
        state = (
            PROVENANCE_POLICY.dirty_state
            if provenance.dirty else PROVENANCE_POLICY.clean_state
        )
        print(f"  Git revision     : {provenance.revision} ({state})", flush=True)
    elif provenance.dirty is not None:
        print(
            f"  Git working tree : "
            f"{PROVENANCE_POLICY.dirty_state if provenance.dirty else PROVENANCE_POLICY.clean_state}",
            flush=True,
        )
    if provenance.mode == PROVENANCE_POLICY.shared_folder_mode:
        print(f"  local action     : {local_action}", flush=True)


def _print_banner() -> None:
    """Print the sbk-analytics ASCII art banner to stderr."""
    banner_path = Path(__file__).parent / "banner.txt"
    try:
        banner = banner_path.read_text(encoding="utf-8")
        print(banner.format(version=__version__), file=sys.stderr, flush=True)
    except Exception:
        # Fallback if banner file is missing
        print(
            f"{APPLICATION.name} version: {__version__}",
            file=sys.stderr,
            flush=True,
        )


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
        nodes = _parse_nodes(params.get(SBK_INTERFACE_POLICY.nodes_option))
        user = str(
            params.get(SBK_INTERFACE_POLICY.gem_user_option, "")
        ).strip()
        password = str(
            params.get(SBK_INTERFACE_POLICY.gem_password_option, "")
        ).strip()
        try:
            port = int(
                params.get(
                    SBK_INTERFACE_POLICY.gem_port_option,
                    SSH_POLICY.default_port,
                )
            )
        except (TypeError, ValueError):
            port = SSH_POLICY.default_port
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
    launcher_root = os.environ.get("SBK_ANALYTICS_SOURCE_ROOT")
    if launcher_root:
        launcher_file = Path(launcher_root) / "sbk-config.env"
        if launcher_file.is_file():
            return launcher_file
    source_tree_file = Path(__file__).resolve().parent.parent / "sbk-config.env"
    if source_tree_file.is_file():
        return source_tree_file
    return Path(__file__).resolve().parent / "default-sbk-config.env"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=APPLICATION.command_name,
        description=(
            "Orchestrate multiple SBK (sbk-yal / sbk-gem-yal) benchmark runs and "
            "feed the resulting CSVs into sbk-charts for combined Excel + AI analytics."
        ),
    )
    p.add_argument(
        "--version",
        action="version",
        version=f"{APPLICATION.name} {__version__}",
    )
    p.add_argument(
        "command", nargs="?", choices=("run", "deps", "config"), default="run",
        help="run benchmarks (default), inspect dependencies, or create config",
    )
    p.add_argument(
        "subcommand", nargs="?", choices=("doctor", "status", "init"),
        help="deps: doctor/status; config: init",
    )
    p.add_argument(
        "-p",
        "--properties",
        type=Path,
        default=None,
        help=(
            "SBK configuration file (key=value): sbk.version, "
            "sbk-charts.version, downloads.folder, sbk.local.folder, "
            "sbk-charts.local.folder, etc. Defaults to the bundled "
            "<project>/sbk-config.env shipped with sbk-analytics."
        ),
    )
    p.add_argument(
        "-c",
        "--config",
        required=False,
        type=Path,
        help="Input YML with the 8 parameter groups (mode, sbk, classes, ...)",
    )
    p.add_argument("--sbk-local", type=Path, help="Local SBK distribution or checkout")
    p.add_argument(
        "--sbk-charts-local", type=Path,
        help="Local sbk-charts checkout or environment",
    )
    p.add_argument(
        "--sbk-charts-executable", type=Path,
        help="Direct path to a local sbk-charts executable",
    )
    p.add_argument(
        "--downloads-folder", type=Path,
        help="Managed package cache (overrides properties and environment)",
    )
    p.add_argument(
        "--resolve-only", action="store_true",
        help="Validate and resolve dependencies without running a benchmark",
    )
    p.add_argument("--json", action="store_true", help="Also print a JSON summary")
    p.add_argument(
        "--output", type=Path, default=Path("sbk-config.local.env"),
        help="Output path for 'config init'",
    )
    p.add_argument(
        "--profile", choices=("local",), default="local",
        help="Configuration profile for 'config init' (default: local)",
    )
    p.add_argument(
        "-w",
        "--work-dir",
        type=Path,
        default=None,
        help=(
            "Working directory for generated YAMLs, CSV files, per-class logs "
            "and the final Excel report. Precedence: this flag > the input "
            "YAML's `workdir:` key > "
            f"{RUNTIME_POLICY.configuration.default_workdir}."
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


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _apply_overrides(versions, args):
    """Apply the documented CLI > environment > properties precedence."""
    sbk_local = args.sbk_local or _env_path("SBK_LOCAL_FOLDER") or versions.sbk_local_folder
    charts_local = (
        args.sbk_charts_local or _env_path("SBK_CHARTS_LOCAL_FOLDER")
        or versions.sbk_charts_local_folder
    )
    charts_executable = (
        args.sbk_charts_executable
        or _env_path("SBK_CHARTS_LOCAL_EXECUTABLE")
        or versions.sbk_charts_local_executable
    )
    # An explicitly configured properties value intentionally wins over the
    # legacy environment cache; the CLI always wins over both.
    downloads = args.downloads_folder or versions.downloads_folder
    return replace(
        versions, sbk_local_folder=sbk_local,
        sbk_charts_local_folder=charts_local,
        sbk_charts_local_executable=charts_executable,
        downloads_folder=downloads,
    )


def _tls_verify(versions) -> bool | str:
    if not versions.ssl_verify:
        return False
    if versions.ssl_ca_bundle is None:
        return True
    if not versions.ssl_ca_bundle.is_file():
        raise ConfigurationError(
            f"ssl.ca.bundle does not exist: {versions.ssl_ca_bundle}"
        )
    return str(versions.ssl_ca_bundle)


def _dependency_summary(sbk, charts, versions) -> dict:
    return {
        "sbk": {"source": sbk.source.value, "home": str(sbk.home),
                "executable": str(sbk.sbk_yal),
                "detected_version": sbk.detected_version,
                "provenance": (
                    sbk.provenance.as_dict() if sbk.provenance else None
                )},
        "sbk_charts": {
            "source": charts.source.value, "home": str(charts.venv_dir),
            "executable": str(charts.cli),
            "detected_version": charts.detected_version,
            "provenance": (
                charts.provenance.as_dict() if charts.provenance else None
            ),
        },
        "downloads_folder": str(versions.downloads_folder) if versions.downloads_folder else None,
        "ssl_verify": versions.ssl_verify,
    }


def _dependency_summary_sbk(sbk) -> dict:
    return {
        "source": sbk.source.value,
        "home": str(sbk.home),
        "executable": str(sbk.sbk_yal),
        "detected_version": sbk.detected_version,
        "provenance": sbk.provenance.as_dict() if sbk.provenance else None,
    }


def _dependency_status(versions) -> dict:
    """Report configured selections and cache markers without mutation/network."""
    root = versions.downloads_folder or cache_root()
    sbk_cache = (
        root / versions.sbk if versions.downloads_folder
        else root / SBK_ARTIFACT.cache_namespace / versions.sbk
    )
    charts_cache = (
        root / SBK_CHARTS_ARTIFACT.cache_namespace / versions.sbk_charts
    )
    sbk_metadata = managed_metadata(sbk_cache)
    charts_metadata = managed_metadata(charts_cache)
    sbk_shared = (
        inspect_shared_sbk(versions.sbk_local_folder)
        if versions.sbk_local_folder is not None else None
    )
    charts_shared = (
        inspect_shared_sbk_charts(
            versions.sbk_charts_local_folder,
            executable=versions.sbk_charts_local_executable,
        )
        if (
            versions.sbk_charts_local_folder is not None
            or versions.sbk_charts_local_executable is not None
        ) else None
    )
    status = {
        "sbk": {
            "selection": (
                PROVENANCE_POLICY.shared_folder_mode
                if sbk_shared is not None
                else PROVENANCE_POLICY.github_release_mode
            ),
            "configured_local": str(versions.sbk_local_folder)
            if versions.sbk_local_folder else None,
            "shared_folder": sbk_shared,
            "repository_url": versions.sbk_url,
            "release_tag": versions.sbk,
            "managed_cache": str(sbk_cache),
            "cache_complete": (
                sbk_cache / CACHE_POLICY.completion_marker
            ).is_file(),
            "cache_metadata": sbk_metadata or None,
        },
        "sbk_charts": {
            "selection": (
                PROVENANCE_POLICY.shared_folder_mode
                if charts_shared is not None
                else PROVENANCE_POLICY.github_release_mode
            ),
            "configured_local": str(versions.sbk_charts_local_folder)
            if versions.sbk_charts_local_folder else None,
            "configured_executable": str(versions.sbk_charts_local_executable)
            if versions.sbk_charts_local_executable else None,
            "shared_folder": charts_shared,
            "repository_url": versions.sbk_charts_url,
            "release_tag": versions.sbk_charts,
            "managed_cache": str(charts_cache),
            "cache_complete": (
                charts_cache / CACHE_POLICY.completion_marker
            ).is_file(),
            "cache_metadata": charts_metadata or None,
        },
        "jdk": {
            "managed_cache": str(versions.jdk_folder / versions.sbk_jdk),
            "cache_complete": (
                versions.jdk_folder / versions.sbk_jdk /
                CACHE_POLICY.completion_marker
            ).is_file(),
        },
        "ssl_verify": versions.ssl_verify,
    }
    return status


def _emit_json(stream, payload: dict) -> None:
    if stream is not None:
        print(json.dumps(payload, indent=2, sort_keys=True), file=stream)


def _print_dependency_status(status: dict) -> None:
    """Render the read-only dependency report without opaque nested dicts."""
    print("Dependency status (read-only; use 'deps doctor' for readiness):")
    for key, label in (("sbk", "SBK"), ("sbk_charts", "sbk-charts")):
        item = status[key]
        print(f"\n{label}:")
        print(f"  selection        : {item['selection']}")
        if item["selection"] == PROVENANCE_POLICY.shared_folder_mode:
            shared = item.get("shared_folder") or {}
            print(f"  configured path  : {shared.get('configured_location')}")
            print(f"  valid            : {shared.get('valid', False)}")
            if shared.get("layout"):
                print(f"  layout           : {shared['layout']}")
            if shared.get("resolved_location"):
                print(f"  resolved path    : {shared['resolved_location']}")
            executable = shared.get("sbk_yal") or shared.get("executable")
            if executable:
                print(f"  executable       : {executable}")
            if shared.get("revision"):
                state = (
                    PROVENANCE_POLICY.dirty_state
                    if shared.get("dirty") else PROVENANCE_POLICY.clean_state
                )
                print(f"  Git revision     : {shared['revision']} ({state})")
            if shared.get("error"):
                print(f"  error            : {shared['error']}")
            action = (
                PROVENANCE_POLICY.sbk_status_action
                if key == SBK_ARTIFACT.key
                else PROVENANCE_POLICY.charts_status_action
            )
            print(f"  status action    : {action}")
        else:
            print(f"  repository       : {item['repository_url']}")
            print(f"  release tag      : {item['release_tag']}")
        print(f"  managed cache    : {item['managed_cache']}")
        print(f"  cache complete   : {item['cache_complete']}")
        metadata = item.get("cache_metadata") or {}
        if metadata.get("asset"):
            print(f"  cached asset     : {metadata['asset']}")
        digest = metadata.get("sha256") or metadata.get("source_sha256")
        if digest:
            print(f"  cached SHA-256   : {digest}")
    jdk = status["jdk"]
    print("\nJDK:")
    print(f"  managed cache    : {jdk['managed_cache']}")
    print(f"  cache complete   : {jdk['cache_complete']}")
    print(f"\nTLS verification  : {status['ssl_verify']}")


def _init_local_config(output: Path) -> int:
    if output.exists():
        raise ConfigurationError(f"refusing to overwrite existing file: {output}")
    template = _bundled_versions_file().read_text(encoding="utf-8")
    output.write_text(template, encoding="utf-8")
    print(f"Created local configuration: {output}")
    print("Edit sbk.local.folder and/or sbk-charts.local.folder before use.")
    return EXIT_CODES.success


def _cleanup_benchmark_data(cfg, work: Path) -> list[Path]:
    """Remove only explicitly configured file-driver data contained in workdir."""
    work_root = work.resolve()
    removed: list[Path] = []
    for instance in cfg.instances:
        if instance.class_name.lower() != "file":
            continue
        raw = instance.params.get("file") or instance.params.get("fname")
        if not raw:
            continue
        path = Path(str(raw)).expanduser()
        path = (work_root / path).resolve() if not path.is_absolute() else path.resolve()
        if path == work_root or work_root not in path.parents:
            log.warning("cleanup skipped path outside workdir: %s", path)
            continue
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _setup_logging(verbosity: int) -> None:
    level = logging.WARNING - DISPLAY_POLICY.logging_verbosity_step * verbosity
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



def _execute(args: argparse.Namespace, json_stream=None) -> int:
    _print_banner()
    _setup_logging(args.verbose)

    if args.command == "config":
        if args.subcommand != "init":
            raise ConfigurationError("config requires the 'init' subcommand")
        rc = _init_local_config(args.output)
        _emit_json(json_stream, {
            "status": "ok", "command": "config init",
            "output": str(args.output), "exit_code": rc,
        })
        return rc
    if args.command == "deps" and args.subcommand not in ("doctor", "status"):
        raise ConfigurationError("deps requires the 'doctor' or 'status' subcommand")
    if args.command == "run" and args.config is None and not args.resolve_only:
        raise ConfigurationError("run requires -c / --config")

    properties_path = args.properties or _bundled_versions_file()
    if not properties_path.is_file():
        raise SystemExit(
            f"versions properties file not found: {properties_path}\n"
            f"either pass -p / --properties or restore the bundled "
            f"{_bundled_versions_file()}"
        )
    versions = _apply_overrides(parse_properties(properties_path), args)
    if not versions.sbk and versions.sbk_local_folder is None:
        raise ConfigurationError(
            "sbk.version is required when no local SBK folder is selected"
        )
    if (not versions.sbk_charts and versions.sbk_charts_local_folder is None
            and versions.sbk_charts_local_executable is None):
        raise ConfigurationError(
            "sbk-charts.version is required when no local sbk-charts is selected"
        )
    verify = _tls_verify(versions)
    log.info("using properties file: %s", properties_path)
    if args.command == "deps" and args.subcommand == "status":
        status = _dependency_status(versions)
        if json_stream is not None:
            _emit_json(json_stream, status)
        else:
            _print_dependency_status(status)
        return EXIT_CODES.success
    cfg = load_config(args.config) if args.config is not None else None

    if versions.sbk_local_folder is not None:
        log.info("SBK local folder: %s", versions.sbk_local_folder)
    else:
        log.info("SBK: %s @ %s", versions.sbk_url, versions.sbk)
    if (versions.sbk_charts_local_folder is not None
            or versions.sbk_charts_local_executable is not None):
        log.info(
            "sbk-charts local selection: %s",
            versions.sbk_charts_local_executable
            or versions.sbk_charts_local_folder,
        )
    else:
        log.info(
            "sbk-charts: %s @ %s",
            versions.sbk_charts_url,
            versions.sbk_charts,
        )
    log.info(
        "mode=%s instances=%d uses_gem=%s",
        cfg.mode if cfg else "dependency-check",
        len(cfg.instances) if cfg else 0,
        cfg.uses_gem if cfg else False,
    )
    for inst in cfg.instances if cfg else []:
        log.info("  - %s (class=%s) params=%s", inst.name, inst.class_name, inst.params)

    # Working directory: precedence is
    #   1. -w / --work-dir CLI flag
    #   2. workdir: in the input YAML (just after `mode:`)
    #   3. the centralized configuration policy default
    work = None
    if cfg is not None:
        work = args.work_dir if args.work_dir is not None else Path(cfg.workdir)
        work.mkdir(parents=True, exist_ok=True)
        log.info("work dir: %s", work.resolve())
        usage = shutil.disk_usage(work)
        gibibyte = DISPLAY_POLICY.bytes_per_kibibyte ** 3
        print(
            f"Filesystem free space: {usage.free / gibibyte:.2f} GiB",
            flush=True,
        )

    # 1. Resolve the required JDK (used via SBK_JAVA_HOME), SBK, and sbk-charts.
    #    ensure_jdk() first checks the existing SBK_JAVA_HOME / JAVA_HOME /
    #    `java` on PATH for a matching major version; only downloads if none
    #    match. The user pins the required major version in sbk-config.env via
    #    `sbk.jdk.version=...` (with a centralized runtime-policy default).
    print("\n=== Resolving dependencies ===", flush=True)
    print(
        "Explicit local folders take priority; other missing dependencies "
        "are downloaded and cached.",
        flush=True,
    )
    
    # Validate an authoritative local SBK before probing or downloading a JDK.
    sbk = ensure_sbk(
        versions.sbk,
        repo=versions.sbk_repo,
        downloads_folder=versions.downloads_folder,
        ssl_verify=verify,
        local_folder=versions.sbk_local_folder,
        require_gem=cfg.uses_gem if cfg else False,
        version_policy=versions.sbk_version_policy,
    )
    _print_sbk_resolution(sbk, versions.sbk)

    jdk = ensure_jdk(
        versions.sbk_jdk, jdk_folder=versions.jdk_folder, ssl_verify=verify
    )
    log.info("JDK %s home: %s", versions.sbk_jdk, jdk.home)
    print(f"[ok] JDK {versions.sbk_jdk} ready at {jdk.home}", flush=True)

    if args.command == "deps" or args.resolve_only:
        charts = ensure_sbk_charts(
            versions.sbk_charts, repo_url=versions.sbk_charts_url,
            source_sha256=versions.sbk_charts_sha256,
            downloads_folder=versions.downloads_folder, ssl_verify=verify,
            local_folder=versions.sbk_charts_local_folder,
            local_executable=versions.sbk_charts_local_executable,
            version_policy=versions.sbk_charts_version_policy,
            preflight=args.command == "deps" and args.subcommand == "doctor",
        )
        _print_charts_resolution(charts, versions.sbk_charts)
        summary = _dependency_summary(sbk, charts, versions)
        summary["status"] = "ok"
        summary["exit_code"] = EXIT_CODES.success
        _emit_json(json_stream, summary)
        print("\nDependency check passed.", flush=True)
        return EXIT_CODES.success

    assert cfg is not None and work is not None

    executable = sbk.sbk_gem_yal if cfg.uses_gem else sbk.sbk_yal
    executables = {
        inst.name: sbk.sbk_gem_yal if inst.uses_gem else sbk.sbk_yal
        for inst in cfg.instances
    }
    log.info("default SBK executable: %s", executable)
    for inst in cfg.instances:
        log.info("SBK executable for %s: %s", inst.name, executables[inst.name])

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
        forward_logs=args.forward_logs, executables=executables,
        output_to_stderr=json_stream is not None,
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
        _emit_json(json_stream, {
            "status": "failed", "exit_code": EXIT_CODES.no_usable_csv,
            "reason": "no usable CSV input",
            "sbk": _dependency_summary_sbk(sbk),
            "successful_instances": [],
            "failed_instances": [r.class_name for r in failed],
        })
        return EXIT_CODES.no_usable_csv

    if failed:
        print(
            f"WARNING: {len(failed)} of {len(results)} SBK runs failed; "
            f"continuing with {len(succeeded)} fresh CSV(s) and "
            f"{len(extra_csvs)} pre-existing use_files.",
            file=sys.stderr,
            flush=True,
        )

    # 4. Resolve sbk-charts lazily, only after usable CSV input exists.
    charts = ensure_sbk_charts(
        versions.sbk_charts, repo_url=versions.sbk_charts_url,
        source_sha256=versions.sbk_charts_sha256,
        downloads_folder=versions.downloads_folder, ssl_verify=verify,
        local_folder=versions.sbk_charts_local_folder,
        local_executable=versions.sbk_charts_local_executable,
        version_policy=versions.sbk_charts_version_policy,
    )
    _print_charts_resolution(charts, versions.sbk_charts)

    # 5. sbk-charts (once)
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

    rc = run_sbk_charts(
        charts, cfg, csv_paths, output_xlsx, work_dir=work,
        output_to_stderr=json_stream is not None,
    )
    if rc != 0:
        log.error("sbk-charts exited with rc=%s", rc)
        _emit_json(json_stream, {
            **_dependency_summary(sbk, charts, versions),
            "status": "failed", "exit_code": rc,
            "reason": "sbk-charts failed",
        })
        return rc
    if not output_xlsx.exists():
        log.error("sbk-charts did not produce expected output: %s", output_xlsx)
        _emit_json(json_stream, {
            **_dependency_summary(sbk, charts, versions),
            "status": "failed", "exit_code": EXIT_CODES.missing_output,
            "reason": "expected output was not produced",
        })
        return EXIT_CODES.missing_output

    # 6. Append system sheet (one row per distinct host: local + remote nodes
    #    visited by sbk-gem-yal instances).
    try:
        sources = _build_system_sources(succeeded)
        append_system_sheet(output_xlsx, sources=sources)
    except Exception as e:
        log.error("failed to append system sheet: %s", e)
        _emit_json(json_stream, {
            **_dependency_summary(sbk, charts, versions),
            "status": "failed", "exit_code": EXIT_CODES.system_info_failure,
            "reason": "failed to append system sheet",
        })
        return EXIT_CODES.system_info_failure

    summary = {
            **_dependency_summary(sbk, charts, versions),
            "status": "ok",
            "exit_code": EXIT_CODES.success,
            "output": str(output_xlsx),
            "successful_instances": [r.class_name for r in succeeded],
            "failed_instances": [r.class_name for r in failed],
    }
    removed: list[Path] = []
    if cfg.cleanup == "on-success":
        removed = _cleanup_benchmark_data(cfg, work)
        print(f"Cleanup on success: removed {len(removed)} benchmark data path(s).")
    usage = shutil.disk_usage(work)
    summary["cleanup"] = {
        "policy": cfg.cleanup,
        "removed_paths": [str(path) for path in removed]
        if cfg.cleanup == "on-success" else [],
    }
    summary["filesystem_free_bytes_after"] = usage.free
    _emit_json(json_stream, summary)
    gibibyte = DISPLAY_POLICY.bytes_per_kibibyte ** 3
    print(f"Filesystem free space after run: {usage.free / gibibyte:.2f} GiB")
    print(f"\nDone. Output: {output_xlsx}", flush=True)
    return EXIT_CODES.success


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with concise expected errors and opt-in tracebacks."""
    args = _parse_args(argv)
    machine_stdout = sys.stdout if args.json else None
    try:
        with child_process_cleanup():
            if machine_stdout is not None:
                with redirect_stdout(sys.stderr):
                    return _execute(args, json_stream=machine_stdout)
            return _execute(args)
    except ProcessExit as exc:
        exit_code = int(exc.code)
        message = f"terminated by signal {exc.signum}"
        print(f"ERROR: {message}", file=sys.stderr)
        _emit_json(machine_stdout, {
            "status": "error", "exit_code": exit_code,
            "error": message, "error_type": exc.__class__.__name__,
            "signal": exc.signum,
        })
        return exit_code
    except (SbkAnalyticsError, OSError, KeyError, ValueError, RuntimeError,
            subprocess.CalledProcessError) as exc:
        verbosity = args.verbose
        if verbosity >= 2:
            traceback.print_exc()
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
            print("Run with -vv for a traceback.", file=sys.stderr)
        _emit_json(machine_stdout, {
            "status": "error", "exit_code": EXIT_CODES.handled_error,
            "error": str(exc), "error_type": exc.__class__.__name__,
        })
        return EXIT_CODES.handled_error


if __name__ == "__main__":
    raise SystemExit(main())
