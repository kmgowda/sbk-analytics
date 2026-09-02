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
from .lifecycle import inspect_records
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
from .workflow import WorkflowServices, execute_workflow

log = logging.getLogger(APPLICATION.name)
CACHE_POLICY = RUNTIME_POLICY.cache
EXIT_CODES = RUNTIME_POLICY.exit_codes
PROVENANCE_POLICY = RUNTIME_POLICY.provenance
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface
DISPLAY_POLICY = RUNTIME_POLICY.display
SSH_POLICY = RUNTIME_POLICY.ssh
SYSTEM_INFO_POLICY = RUNTIME_POLICY.system_info
ENVIRONMENT_POLICY = RUNTIME_POLICY.environment
LIFECYCLE_POLICY = RUNTIME_POLICY.lifecycle
CACHE_METADATA_POLICY = RUNTIME_POLICY.cache_metadata
CONFIGURATION_POLICY = RUNTIME_POLICY.configuration
CLI_POLICY = RUNTIME_POLICY.cli
DIAGNOSTIC_FIELDS = RUNTIME_POLICY.diagnostics


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
        print(
            "  detected version : "
            f"{install.detected_version or DISPLAY_POLICY.unknown_value}",
            flush=True,
        )
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
            "  detected version : "
            f"{install.detected_version or DISPLAY_POLICY.unknown_value}",
            flush=True,
        )
        print(f"  configured version: {version} (policy applies)", flush=True)
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
                SYSTEM_INFO_POLICY.source_kind_field:
                    SYSTEM_INFO_POLICY.remote_source,
                SYSTEM_INFO_POLICY.source_node_field: node,
                SYSTEM_INFO_POLICY.source_user_field: user,
                SYSTEM_INFO_POLICY.source_password_field: password,
                SYSTEM_INFO_POLICY.source_port_field: port,
                SYSTEM_INFO_POLICY.source_instances_field: [],
            })
            entry[SYSTEM_INFO_POLICY.source_instances_field].append(
                r.class_name
            )

    sources: list[dict] = []
    if local_instances:
        sources.append({
            SYSTEM_INFO_POLICY.source_kind_field:
                SYSTEM_INFO_POLICY.local_source,
            SYSTEM_INFO_POLICY.source_instances_field: local_instances,
        })
    sources.extend(remote_map.values())
    return sources


def _bundled_versions_file() -> Path:
    """Return the project-bundled `sbk-config.env` shipped next to this package.

    The file lives at the repository root (`<project>/sbk-config.env`).
    """
    launcher_root = os.environ.get(ENVIRONMENT_POLICY.source_root)
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
        CLI_POLICY.command_destination,
        nargs="?", choices=CLI_POLICY.commands,
        default=CLI_POLICY.run_command,
        help="run benchmarks (default), inspect dependencies, or create config",
    )
    p.add_argument(
        CLI_POLICY.subcommand_destination,
        nargs="?", choices=CLI_POLICY.subcommands,
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
        help="Input YML with the orchestration groups (mode, sbk, benchmarks, ...)",
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
        "--output", type=Path, default=Path(CLI_POLICY.local_config_filename),
        help="Output path for 'config init'",
    )
    p.add_argument(
        "--profile", choices=(CLI_POLICY.local_profile,),
        default=CLI_POLICY.local_profile,
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
    sbk_local = (
        args.sbk_local
        or _env_path(ENVIRONMENT_POLICY.sbk_local_folder)
        or versions.sbk_local_folder
    )
    charts_local = (
        args.sbk_charts_local
        or _env_path(ENVIRONMENT_POLICY.charts_local_folder)
        or versions.sbk_charts_local_folder
    )
    charts_executable = (
        args.sbk_charts_executable
        or _env_path(ENVIRONMENT_POLICY.charts_local_executable)
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
        DIAGNOSTIC_FIELDS.sbk: {
                DIAGNOSTIC_FIELDS.source: sbk.source.value,
                DIAGNOSTIC_FIELDS.home: str(sbk.home),
                DIAGNOSTIC_FIELDS.executable: str(sbk.sbk_yal),
                DIAGNOSTIC_FIELDS.detected_version: sbk.detected_version,
                DIAGNOSTIC_FIELDS.provenance: (
                    sbk.provenance.as_dict() if sbk.provenance else None
                )},
        DIAGNOSTIC_FIELDS.charts: {
            DIAGNOSTIC_FIELDS.source: charts.source.value,
            DIAGNOSTIC_FIELDS.home: str(charts.venv_dir),
            DIAGNOSTIC_FIELDS.executable: str(charts.cli),
            DIAGNOSTIC_FIELDS.detected_version: charts.detected_version,
            DIAGNOSTIC_FIELDS.provenance: (
                charts.provenance.as_dict() if charts.provenance else None
            ),
        },
        DIAGNOSTIC_FIELDS.downloads_folder: (
            str(versions.downloads_folder)
            if versions.downloads_folder else None
        ),
        DIAGNOSTIC_FIELDS.ssl_verify: versions.ssl_verify,
        DIAGNOSTIC_FIELDS.lifecycle: inspect_records(),
    }


def _dependency_summary_sbk(sbk) -> dict:
    return {
        DIAGNOSTIC_FIELDS.source: sbk.source.value,
        DIAGNOSTIC_FIELDS.home: str(sbk.home),
        DIAGNOSTIC_FIELDS.executable: str(sbk.sbk_yal),
        DIAGNOSTIC_FIELDS.detected_version: sbk.detected_version,
        DIAGNOSTIC_FIELDS.provenance: (
            sbk.provenance.as_dict() if sbk.provenance else None
        ),
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
        DIAGNOSTIC_FIELDS.sbk: {
            DIAGNOSTIC_FIELDS.selection: (
                PROVENANCE_POLICY.shared_folder_mode
                if sbk_shared is not None
                else PROVENANCE_POLICY.github_release_mode
            ),
            DIAGNOSTIC_FIELDS.configured_local: str(versions.sbk_local_folder)
            if versions.sbk_local_folder else None,
            DIAGNOSTIC_FIELDS.shared_folder: sbk_shared,
            DIAGNOSTIC_FIELDS.repository_url: versions.sbk_url,
            DIAGNOSTIC_FIELDS.release_tag: versions.sbk,
            DIAGNOSTIC_FIELDS.managed_cache: str(sbk_cache),
            DIAGNOSTIC_FIELDS.cache_complete: (
                sbk_cache / CACHE_POLICY.completion_marker
            ).is_file(),
            DIAGNOSTIC_FIELDS.cache_metadata: sbk_metadata or None,
        },
        DIAGNOSTIC_FIELDS.charts: {
            DIAGNOSTIC_FIELDS.selection: (
                PROVENANCE_POLICY.shared_folder_mode
                if charts_shared is not None
                else PROVENANCE_POLICY.github_release_mode
            ),
            DIAGNOSTIC_FIELDS.configured_local: str(versions.sbk_charts_local_folder)
            if versions.sbk_charts_local_folder else None,
            DIAGNOSTIC_FIELDS.configured_executable: str(
                versions.sbk_charts_local_executable
            )
            if versions.sbk_charts_local_executable else None,
            DIAGNOSTIC_FIELDS.shared_folder: charts_shared,
            DIAGNOSTIC_FIELDS.repository_url: versions.sbk_charts_url,
            DIAGNOSTIC_FIELDS.release_tag: versions.sbk_charts,
            DIAGNOSTIC_FIELDS.managed_cache: str(charts_cache),
            DIAGNOSTIC_FIELDS.cache_complete: (
                charts_cache / CACHE_POLICY.completion_marker
            ).is_file(),
            DIAGNOSTIC_FIELDS.cache_metadata: charts_metadata or None,
        },
        DIAGNOSTIC_FIELDS.jdk: {
            DIAGNOSTIC_FIELDS.managed_cache: str(
                versions.jdk_folder / versions.sbk_jdk
            ),
            DIAGNOSTIC_FIELDS.cache_complete: (
                versions.jdk_folder / versions.sbk_jdk /
                CACHE_POLICY.completion_marker
            ).is_file(),
        },
        DIAGNOSTIC_FIELDS.ssl_verify: versions.ssl_verify,
        DIAGNOSTIC_FIELDS.lifecycle: inspect_records(),
    }
    return status


def _emit_json(stream, payload: dict) -> None:
    if stream is not None:
        print(json.dumps(payload, indent=2, sort_keys=True), file=stream)


def _print_dependency_status(status: dict) -> None:
    """Render the read-only dependency report without opaque nested dicts."""
    print("Dependency status (read-only; use 'deps doctor' for readiness):")
    for key, label in (
        (DIAGNOSTIC_FIELDS.sbk, SBK_ARTIFACT.display_name),
        (DIAGNOSTIC_FIELDS.charts, SBK_CHARTS_ARTIFACT.display_name),
    ):
        item = status[key]
        print(f"\n{label}:")
        print(f"  selection        : {item[DIAGNOSTIC_FIELDS.selection]}")
        if (
            item[DIAGNOSTIC_FIELDS.selection]
            == PROVENANCE_POLICY.shared_folder_mode
        ):
            shared = item.get(DIAGNOSTIC_FIELDS.shared_folder) or {}
            print(
                "  configured path  : "
                f"{shared.get(DIAGNOSTIC_FIELDS.configured_location)}"
            )
            print(
                "  valid            : "
                f"{shared.get(DIAGNOSTIC_FIELDS.valid, False)}"
            )
            if shared.get(DIAGNOSTIC_FIELDS.layout):
                print(
                    "  layout           : "
                    f"{shared[DIAGNOSTIC_FIELDS.layout]}"
                )
            if shared.get(DIAGNOSTIC_FIELDS.resolved_location):
                print(
                    "  resolved path    : "
                    f"{shared[DIAGNOSTIC_FIELDS.resolved_location]}"
                )
            executable = (
                shared.get(DIAGNOSTIC_FIELDS.sbk_yal)
                or shared.get(DIAGNOSTIC_FIELDS.executable)
            )
            if executable:
                print(f"  executable       : {executable}")
            if shared.get(DIAGNOSTIC_FIELDS.revision):
                state = (
                    PROVENANCE_POLICY.dirty_state
                    if shared.get(DIAGNOSTIC_FIELDS.dirty)
                    else PROVENANCE_POLICY.clean_state
                )
                print(
                    "  Git revision     : "
                    f"{shared[DIAGNOSTIC_FIELDS.revision]} ({state})"
                )
            if shared.get(DIAGNOSTIC_FIELDS.error):
                print(
                    "  error            : "
                    f"{shared[DIAGNOSTIC_FIELDS.error]}"
                )
            action = (
                PROVENANCE_POLICY.sbk_status_action
                if key == SBK_ARTIFACT.key
                else PROVENANCE_POLICY.charts_status_action
            )
            print(f"  status action    : {action}")
        else:
            print(
                "  repository       : "
                f"{item[DIAGNOSTIC_FIELDS.repository_url]}"
            )
            print(
                "  release tag      : "
                f"{item[DIAGNOSTIC_FIELDS.release_tag]}"
            )
        print(
            "  managed cache    : "
            f"{item[DIAGNOSTIC_FIELDS.managed_cache]}"
        )
        print(
            "  cache complete   : "
            f"{item[DIAGNOSTIC_FIELDS.cache_complete]}"
        )
        metadata = item.get(DIAGNOSTIC_FIELDS.cache_metadata) or {}
        asset_field = CACHE_METADATA_POLICY.asset
        if metadata.get(asset_field):
            print(f"  cached asset     : {metadata[asset_field]}")
        digest = (
            metadata.get(CACHE_METADATA_POLICY.sha256)
            or metadata.get(CACHE_METADATA_POLICY.source_sha256)
        )
        if digest:
            print(f"  cached SHA-256   : {digest}")
    jdk = status[DIAGNOSTIC_FIELDS.jdk]
    print("\nJDK:")
    print(
        "  managed cache    : "
        f"{jdk[DIAGNOSTIC_FIELDS.managed_cache]}"
    )
    print(
        "  cache complete   : "
        f"{jdk[DIAGNOSTIC_FIELDS.cache_complete]}"
    )
    print(
        "\nTLS verification  : "
        f"{status[DIAGNOSTIC_FIELDS.ssl_verify]}"
    )
    lifecycle = status[DIAGNOSTIC_FIELDS.lifecycle]
    print("\nWorkload lifecycle:")
    print(f"  registry         : {lifecycle[LIFECYCLE_POLICY.registry_field]}")
    print(f"  active records   : {lifecycle[LIFECYCLE_POLICY.active_field]}")
    print(f"  stale records    : {lifecycle[LIFECYCLE_POLICY.stale_field]}")
    print(
        "  unresolved       : "
        f"{lifecycle[LIFECYCLE_POLICY.unresolved_field]}"
    )


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


def _cleanup_workdir_before_run(
    work: Path,
    *,
    protected_paths: tuple[Path | None, ...] = (),
) -> list[Path]:
    """Remove every entry below workdir after refusing dangerous scopes."""
    work_root = work.expanduser().resolve()
    source_root = os.environ.get(ENVIRONMENT_POLICY.source_root)
    protected = {
        Path(work_root.anchor).resolve(),
        Path.home().resolve(),
        Path.cwd().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        Path(__file__).resolve().parent.parent,
    }
    if source_root:
        protected.add(Path(source_root).expanduser().resolve())
    protected.update(
        path.expanduser().resolve()
        for path in protected_paths
        if path is not None
    )
    for path in protected:
        if work_root == path or work_root in path.parents:
            raise ConfigurationError(
                f"{CONFIGURATION_POLICY.cleanup_before_run_keys[0]} refuses "
                "workdir because it contains "
                f"protected path {path}: {work_root}"
            )
    if not work_root.is_dir():
        raise ConfigurationError(
            f"{CONFIGURATION_POLICY.cleanup_before_run_keys[0]} requires an "
            f"existing directory: {work_root}"
        )

    removed: list[Path] = []
    for entry in work_root.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)
        removed.append(entry)
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

    if args.command == CLI_POLICY.configuration_command:
        if args.subcommand != CLI_POLICY.initialize_subcommand:
            raise ConfigurationError("config requires the 'init' subcommand")
        rc = _init_local_config(args.output)
        _emit_json(json_stream, {
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.success_status,
            DIAGNOSTIC_FIELDS.command: (
                f"{CLI_POLICY.configuration_command} "
                f"{CLI_POLICY.initialize_subcommand}"
            ),
            DIAGNOSTIC_FIELDS.output: str(args.output),
            DIAGNOSTIC_FIELDS.exit_code: rc,
        })
        return rc
    if (
        args.command == CLI_POLICY.dependencies_command
        and args.subcommand not in (
            CLI_POLICY.doctor_subcommand,
            CLI_POLICY.status_subcommand,
        )
    ):
        raise ConfigurationError("deps requires the 'doctor' or 'status' subcommand")
    if (
        args.command == CLI_POLICY.run_command
        and args.config is None
        and not args.resolve_only
    ):
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
    if (
        args.command == CLI_POLICY.dependencies_command
        and args.subcommand == CLI_POLICY.status_subcommand
    ):
        status = _dependency_status(versions)
        if json_stream is not None:
            _emit_json(json_stream, status)
        else:
            _print_dependency_status(status)
        return EXIT_CODES.success
    services = WorkflowServices(
        load_config=load_config,
        ensure_sbk=ensure_sbk,
        ensure_jdk=ensure_jdk,
        ensure_sbk_charts=ensure_sbk_charts,
        _print_sbk_resolution=_print_sbk_resolution,
        _print_charts_resolution=_print_charts_resolution,
        _cleanup_workdir_before_run=_cleanup_workdir_before_run,
        generate_instance_yaml=generate_instance_yaml,
        run_jobs=run_jobs,
        run_sbk_charts=run_sbk_charts,
        append_system_sheet=append_system_sheet,
        _build_system_sources=_build_system_sources,
        _dependency_summary=_dependency_summary,
        _dependency_summary_sbk=_dependency_summary_sbk,
        _cleanup_benchmark_data=_cleanup_benchmark_data,
        _emit_json=_emit_json,
    )
    return execute_workflow(
        args,
        properties_path=properties_path,
        versions=versions,
        verify=verify,
        services=services,
        json_stream=json_stream,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the CLI with concise expected errors and opt-in tracebacks."""
    args = _parse_args(argv)
    machine_stdout = sys.stdout if args.json else None
    try:
        reconcile = (
            args.command == CLI_POLICY.run_command
            or args.resolve_only
            or (
                args.command == CLI_POLICY.dependencies_command
                and args.subcommand == CLI_POLICY.doctor_subcommand
            )
        )
        with child_process_cleanup(reconcile=reconcile):
            if machine_stdout is not None:
                with redirect_stdout(sys.stderr):
                    return _execute(args, json_stream=machine_stdout)
            return _execute(args)
    except ProcessExit as exc:
        exit_code = int(exc.code)
        message = f"terminated by signal {exc.signum}"
        print(f"ERROR: {message}", file=sys.stderr)
        _emit_json(machine_stdout, {
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.error_status,
            DIAGNOSTIC_FIELDS.exit_code: exit_code,
            DIAGNOSTIC_FIELDS.error: message,
            DIAGNOSTIC_FIELDS.error_type: exc.__class__.__name__,
            DIAGNOSTIC_FIELDS.signal: exc.signum,
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
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.error_status,
            DIAGNOSTIC_FIELDS.exit_code: EXIT_CODES.handled_error,
            DIAGNOSTIC_FIELDS.error: str(exc),
            DIAGNOSTIC_FIELDS.error_type: exc.__class__.__name__,
        })
        return EXIT_CODES.handled_error


if __name__ == "__main__":
    raise SystemExit(main())
