#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Composable execution phases for one sbk-analytics workflow."""
from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .policy import APPLICATION, RUNTIME_POLICY

log = logging.getLogger(APPLICATION.name)
EXIT_CODES = RUNTIME_POLICY.exit_codes
DISPLAY_POLICY = RUNTIME_POLICY.display
CLI_POLICY = RUNTIME_POLICY.cli
DIAGNOSTIC_FIELDS = RUNTIME_POLICY.diagnostics
WORKFLOW_POLICY = RUNTIME_POLICY.workflow


@dataclass(frozen=True)
class WorkflowServices:
    """Injected workflow boundaries kept patchable by CLI integration tests."""

    load_config: Callable[..., Any]
    ensure_sbk: Callable[..., Any]
    ensure_jdk: Callable[..., Any]
    ensure_sbk_charts: Callable[..., Any]
    _print_sbk_resolution: Callable[..., Any]
    _print_charts_resolution: Callable[..., Any]
    _cleanup_workdir_before_run: Callable[..., Any]
    generate_instance_yaml: Callable[..., Any]
    run_jobs: Callable[..., Any]
    run_sbk_charts: Callable[..., Any]
    append_system_sheet: Callable[..., Any]
    _build_system_sources: Callable[..., Any]
    _dependency_summary: Callable[..., Any]
    _dependency_summary_sbk: Callable[..., Any]
    _cleanup_benchmark_data: Callable[..., Any]
    _emit_json: Callable[..., Any]


def _prepare_workflow(args: Any, versions: Any, services: WorkflowServices):
    cfg = services.load_config(args.config) if args.config is not None else None

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
        cfg.mode if cfg else WORKFLOW_POLICY.dependency_check_mode,
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
    return cfg, work


def _resolve_dependencies(
    cfg: Any,
    versions: Any,
    verify: bool | str,
    services: WorkflowServices,
):
    # 1. Resolve the required JDK (used via SBK_JAVA_HOME), SBK, and sbk-charts.
    #    services.ensure_jdk() first checks the existing SBK_JAVA_HOME / JAVA_HOME /
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
    sbk = services.ensure_sbk(
        versions.sbk,
        repo=versions.sbk_repo,
        downloads_folder=versions.downloads_folder,
        ssl_verify=verify,
        local_folder=versions.sbk_local_folder,
        require_gem=cfg.uses_gem if cfg else False,
        version_policy=versions.sbk_version_policy,
    )
    services._print_sbk_resolution(sbk, versions.sbk)

    jdk = services.ensure_jdk(
        versions.sbk_jdk, jdk_folder=versions.jdk_folder, ssl_verify=verify
    )
    log.info("JDK %s home: %s", versions.sbk_jdk, jdk.home)
    print(f"[ok] JDK {versions.sbk_jdk} ready at {jdk.home}", flush=True)
    return sbk, jdk


def _complete_dependency_check(
    args: Any,
    sbk: Any,
    versions: Any,
    verify: bool | str,
    services: WorkflowServices,
    json_stream: Any,
) -> int:
    charts = services.ensure_sbk_charts(
        versions.sbk_charts, repo_url=versions.sbk_charts_url,
        source_sha256=versions.sbk_charts_sha256,
        downloads_folder=versions.downloads_folder, ssl_verify=verify,
        local_folder=versions.sbk_charts_local_folder,
        local_executable=versions.sbk_charts_local_executable,
        version_policy=versions.sbk_charts_version_policy,
        preflight=(
            args.command == CLI_POLICY.dependencies_command
            and args.subcommand == CLI_POLICY.doctor_subcommand
        ),
    )
    services._print_charts_resolution(charts, versions.sbk_charts)
    summary = services._dependency_summary(sbk, charts, versions)
    summary[DIAGNOSTIC_FIELDS.status] = CLI_POLICY.success_status
    summary[DIAGNOSTIC_FIELDS.exit_code] = EXIT_CODES.success
    services._emit_json(json_stream, summary)
    print("\nDependency check passed.", flush=True)
    return EXIT_CODES.success


def _run_benchmarks(
    args: Any,
    cfg: Any,
    work: Path,
    properties_path: Path,
    versions: Any,
    sbk: Any,
    jdk: Any,
    services: WorkflowServices,
    json_stream: Any,
):
    executable = sbk.sbk_gem_yal if cfg.uses_gem else sbk.sbk_yal
    executables = {
        inst.name: sbk.sbk_gem_yal if inst.uses_gem else sbk.sbk_yal
        for inst in cfg.instances
    }
    log.info("default SBK executable: %s", executable)
    for inst in cfg.instances:
        log.info("SBK executable for %s: %s", inst.name, executables[inst.name])

    pre_run_removed: list[Path] = []
    if cfg.cleanup_before_run:
        pre_run_removed = services._cleanup_workdir_before_run(
            work,
            protected_paths=(
                args.config,
                properties_path,
                versions.downloads_folder,
                versions.jdk_folder,
                versions.sbk_local_folder,
                versions.sbk_charts_local_folder,
                versions.sbk_charts_local_executable,
                sbk.home,
                jdk.home,
            ),
        )
        print(
            "Pre-run workdir cleanup: removed "
            f"{len(pre_run_removed)} top-level entr"
            f"{'y' if len(pre_run_removed) == 1 else 'ies'} from "
            f"{work.resolve()}.",
            flush=True,
        )

    # 2. Generate per-class YAMLs
    yml_dir = work / WORKFLOW_POLICY.yaml_directory
    csv_dir = work / WORKFLOW_POLICY.csv_directory
    csv_dir.mkdir(parents=True, exist_ok=True)

    jobs: list[tuple[str, Path, Path]] = []
    for inst in cfg.instances:
        csv_path = (
            csv_dir
            / WORKFLOW_POLICY.csv_filename_template.format(name=inst.name)
        ).resolve()
        yml_path = services.generate_instance_yaml(inst, yml_dir, csv_path)
        jobs.append((inst.name, yml_path, csv_path))

    # 3. Run SBK instances
    log_dir = work / WORKFLOW_POLICY.log_directory
    results = services.run_jobs(
        executable, jobs, mode=cfg.mode, log_dir=log_dir, jdk_home=jdk.home,
        forward_logs=args.forward_logs, executables=executables,
        output_to_stderr=json_stream is not None,
    )

    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print("\n=== SBK run summary ===", flush=True)
    for r in results:
        status = (
            WORKFLOW_POLICY.successful_run_status
            if r.ok
            else WORKFLOW_POLICY.failed_run_status_template.format(
                returncode=r.returncode
            )
        )
        extra = f" log={r.log_path}" if r.log_path else ""
        print(f"  {status:14s} instance={r.class_name} csv={r.csv_path}{extra}")
    return succeeded, failed, pre_run_removed


def _collect_extra_csvs(cfg: Any, work: Path) -> list[Path]:
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
    return extra_csvs


def _validate_usable_inputs(
    succeeded: list[Any],
    failed: list[Any],
    extra_csvs: list[Path],
    sbk: Any,
    services: WorkflowServices,
    json_stream: Any,
) -> int | None:
    if not succeeded and not extra_csvs:
        print(
            "All SBK instances failed and no use_files supplied; skipping "
            "sbk-charts as per spec.",
            file=sys.stderr,
            flush=True,
        )
        services._emit_json(json_stream, {
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.failed_status,
            DIAGNOSTIC_FIELDS.exit_code: EXIT_CODES.no_usable_csv,
            DIAGNOSTIC_FIELDS.reason: WORKFLOW_POLICY.no_usable_csv_reason,
            DIAGNOSTIC_FIELDS.sbk: services._dependency_summary_sbk(sbk),
            DIAGNOSTIC_FIELDS.successful_instances: [],
            DIAGNOSTIC_FIELDS.failed_instances: [
                r.class_name for r in failed
            ],
        })
        return EXIT_CODES.no_usable_csv

    if failed:
        total_runs = len(succeeded) + len(failed)
        print(
            f"WARNING: {len(failed)} of {total_runs} SBK runs failed; "
            f"continuing with {len(succeeded)} fresh CSV(s) and "
            f"{len(extra_csvs)} pre-existing use_files.",
            file=sys.stderr,
            flush=True,
        )
    return None


def _publish_report(
    args: Any,
    cfg: Any,
    work: Path,
    versions: Any,
    verify: bool | str,
    sbk: Any,
    succeeded: list[Any],
    failed: list[Any],
    extra_csvs: list[Path],
    pre_run_removed: list[Path],
    services: WorkflowServices,
    json_stream: Any,
) -> int:
    # 4. Resolve sbk-charts lazily, only after usable CSV input exists.
    charts = services.ensure_sbk_charts(
        versions.sbk_charts, repo_url=versions.sbk_charts_url,
        source_sha256=versions.sbk_charts_sha256,
        downloads_folder=versions.downloads_folder, ssl_verify=verify,
        local_folder=versions.sbk_charts_local_folder,
        local_executable=versions.sbk_charts_local_executable,
        version_policy=versions.sbk_charts_version_policy,
    )
    services._print_charts_resolution(charts, versions.sbk_charts)

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

    rc = services.run_sbk_charts(
        charts, cfg, csv_paths, output_xlsx, work_dir=work,
        output_to_stderr=json_stream is not None,
    )
    if rc != 0:
        log.error("sbk-charts exited with rc=%s", rc)
        services._emit_json(json_stream, {
            **services._dependency_summary(sbk, charts, versions),
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.failed_status,
            DIAGNOSTIC_FIELDS.exit_code: rc,
            DIAGNOSTIC_FIELDS.reason: WORKFLOW_POLICY.charts_failure_reason,
        })
        return rc
    if not output_xlsx.exists():
        log.error("sbk-charts did not produce expected output: %s", output_xlsx)
        services._emit_json(json_stream, {
            **services._dependency_summary(sbk, charts, versions),
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.failed_status,
            DIAGNOSTIC_FIELDS.exit_code: EXIT_CODES.missing_output,
            DIAGNOSTIC_FIELDS.reason: WORKFLOW_POLICY.missing_output_reason,
        })
        return EXIT_CODES.missing_output

    # 6. Append system sheet (one row per distinct host: local + remote nodes
    #    visited by sbk-gem-yal instances).
    try:
        sources = services._build_system_sources(succeeded)
        services.append_system_sheet(output_xlsx, sources=sources)
    except Exception as e:
        log.error("failed to append system sheet: %s", e)
        services._emit_json(json_stream, {
            **services._dependency_summary(sbk, charts, versions),
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.failed_status,
            DIAGNOSTIC_FIELDS.exit_code: EXIT_CODES.system_info_failure,
            DIAGNOSTIC_FIELDS.reason:
                WORKFLOW_POLICY.system_info_failure_reason,
        })
        return EXIT_CODES.system_info_failure

    summary = {
            **services._dependency_summary(sbk, charts, versions),
            DIAGNOSTIC_FIELDS.status: CLI_POLICY.success_status,
            DIAGNOSTIC_FIELDS.exit_code: EXIT_CODES.success,
            DIAGNOSTIC_FIELDS.output: str(output_xlsx),
            DIAGNOSTIC_FIELDS.successful_instances: [
                r.class_name for r in succeeded
            ],
            DIAGNOSTIC_FIELDS.failed_instances: [
                r.class_name for r in failed
            ],
    }
    removed: list[Path] = []
    cleanup_on_success = RUNTIME_POLICY.configuration.cleanup_on_success
    if cfg.cleanup == cleanup_on_success:
        removed = services._cleanup_benchmark_data(cfg, work)
        print(f"Cleanup on success: removed {len(removed)} benchmark data path(s).")
    usage = shutil.disk_usage(work)
    summary[DIAGNOSTIC_FIELDS.cleanup] = {
        DIAGNOSTIC_FIELDS.cleanup_policy: cfg.cleanup,
        DIAGNOSTIC_FIELDS.removed_paths: [str(path) for path in removed]
        if cfg.cleanup == cleanup_on_success else [],
        DIAGNOSTIC_FIELDS.cleanup_before_run: cfg.cleanup_before_run,
        DIAGNOSTIC_FIELDS.before_run_removed_entries:
            len(pre_run_removed),
    }
    summary[DIAGNOSTIC_FIELDS.filesystem_free_bytes_after] = usage.free
    services._emit_json(json_stream, summary)
    gibibyte = DISPLAY_POLICY.bytes_per_kibibyte ** 3
    print(f"Filesystem free space after run: {usage.free / gibibyte:.2f} GiB")
    print(f"\nDone. Output: {output_xlsx}", flush=True)
    return EXIT_CODES.success


def execute_workflow(
    args: Any,
    *,
    properties_path: Path,
    versions: Any,
    verify: bool | str,
    services: WorkflowServices,
    json_stream: Any = None,
) -> int:
    """Run the explicit dependency, benchmark, and report phases."""
    cfg, work = _prepare_workflow(args, versions, services)
    sbk, jdk = _resolve_dependencies(cfg, versions, verify, services)
    if args.command == CLI_POLICY.dependencies_command or args.resolve_only:
        return _complete_dependency_check(
            args, sbk, versions, verify, services, json_stream
        )

    assert cfg is not None and work is not None
    succeeded, failed, pre_run_removed = _run_benchmarks(
        args, cfg, work, properties_path, versions, sbk, jdk, services,
        json_stream,
    )
    extra_csvs = _collect_extra_csvs(cfg, work)
    early_exit = _validate_usable_inputs(
        succeeded, failed, extra_csvs, sbk, services, json_stream
    )
    if early_exit is not None:
        return early_exit
    return _publish_report(
        args, cfg, work, versions, verify, sbk, succeeded, failed, extra_csvs,
        pre_run_removed, services, json_stream,
    )
