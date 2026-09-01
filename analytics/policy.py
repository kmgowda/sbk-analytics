#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Canonical runtime policy and managed-artifact metadata.

Keep product-level defaults here instead of scattering them through resolver,
runner, process, and configuration code. Algorithm-local constants should
remain beside their algorithms; values shared across subsystem boundaries or
representing an operational decision belong in this module.

Security compatibility policy
-----------------------------
TLS certificate verification and SSH host-key verification intentionally
default to disabled for compatibility with isolated benchmark labs and private
artifact infrastructure. These defaults trust the configured network and
remote hosts; production or other untrusted environments should enable TLS
verification (and optionally configure a CA bundle). SSH policy should only be
used with dedicated, trusted benchmark nodes unless host-key verification is
enabled here.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ApplicationMetadata:
    """Stable identity for the application itself."""

    name: str
    distribution_name: str
    command_name: str
    repository_url: str


APPLICATION = ApplicationMetadata(
    name="sbk-analytics",
    distribution_name="sbk-analytics",
    command_name="sbk-analytics",
    repository_url="https://github.com/kmgowda/sbk-analytics",
)


@dataclass(frozen=True)
class ArtifactMetadata:
    """Stable identity and filesystem layout for one managed dependency."""

    key: str
    display_name: str
    distribution_name: str
    repository_url: str
    cache_namespace: str
    primary_executable: str
    additional_executables: tuple[str, ...] = ()
    repository_slug: str | None = None
    metadata_url_template: str | None = None
    version_arguments: tuple[str, ...] = ()
    version_pattern: str | None = None

    @property
    def executables(self) -> tuple[str, ...]:
        return (self.primary_executable, *self.additional_executables)


SBK_ARTIFACT = ArtifactMetadata(
    key="sbk",
    display_name="SBK",
    distribution_name="sbk",
    repository_url="https://github.com/kmgowda/SBK",
    cache_namespace="sbk",
    primary_executable="sbk-yal",
    additional_executables=("sbk-gem-yal",),
    repository_slug="kmgowda/SBK",
    version_arguments=("-help",),
    version_pattern=r"SBK(?:-YAL)?\s+Version:\s*([^\s]+)",
)

SBK_CHARTS_ARTIFACT = ArtifactMetadata(
    key="sbk-charts",
    display_name="sbk-charts",
    distribution_name="sbk-charts",
    repository_url="https://github.com/kmgowda/sbk-charts",
    cache_namespace="sbk-charts",
    primary_executable="sbk-charts",
    repository_slug="kmgowda/sbk-charts",
    version_arguments=("-h",),
    version_pattern=r"Sbk Charts Version\s*:\s*(\d+(?:\.\d+)+)",
)

JDK_ARTIFACT = ArtifactMetadata(
    key="jdk",
    display_name="JDK",
    distribution_name="Temurin JDK",
    repository_url="https://github.com/adoptium/temurin-binaries",
    metadata_url_template=(
        "https://api.adoptium.net/v3/assets/latest/{version}/hotspot"
        "?architecture={arch}&image_type=jdk&os={os}&vendor=eclipse"
    ),
    cache_namespace="jdk",
    primary_executable="java",
    version_arguments=("-version",),
    version_pattern=r'(?:openjdk|java)\s+version\s+"([^"]+)"',
)

ARTIFACTS: Mapping[str, ArtifactMetadata] = MappingProxyType({
    artifact.key: artifact
    for artifact in (SBK_ARTIFACT, SBK_CHARTS_ARTIFACT, JDK_ARTIFACT)
})


@dataclass(frozen=True)
class CachePolicy:
    completion_marker: str = ".ok"
    home_pointer: str = ".home"
    metadata_filename: str = "metadata.json"
    default_downloads_folder: str = "./.sbk"
    default_jdk_folder: str = "./.jdk"
    lock_name_template: str = ".{name}.lock"
    install_stage_template: str = ".{name}.install-{pid}"


@dataclass(frozen=True)
class CacheMetadataPolicy:
    """Stable JSON field names shared by installers and diagnostics."""

    dependency: str = "dependency"
    version: str = "version"
    source_url: str = "source_url"
    asset: str = "asset"
    sha256: str = "sha256"
    source_sha256: str = "source_sha256"
    executable: str = "executable"
    executables: str = "executables"
    detected_major: str = "detected_major"
    install_specification: str = "spec"
    installed_at: str = "installed_at"
    sha256_pattern: str = r"[0-9a-f]{64}"


@dataclass(frozen=True)
class DependencyLayoutPolicy:
    """Stable filesystem names used by dependency providers."""

    executable_directory: str = "bin"
    virtual_environment_directory: str = "venv"
    virtual_environment_configuration: str = "pyvenv.cfg"
    python_executable: str = "python"
    extracted_directory: str = "extracted"
    sbk_gradle_install_path: tuple[str, ...] = ("build", "install", "sbk")
    git_metadata: str = ".git"
    git_url_suffix: str = ".git"


@dataclass(frozen=True)
class ArchivePolicy:
    """Supported managed-archive formats and executable publication mode."""

    zip_suffix: str = ".zip"
    tar_suffixes: tuple[str, ...] = (
        ".tar", ".tar.gz", ".tgz", ".tar.bz2",
    )
    member_mode_shift: int = 16
    executable_mode_mask: int = 0o111
    secondary_asset_penalty: int = 10

    @property
    def release_suffixes(self) -> tuple[str, ...]:
        return (*self.tar_suffixes[:3], self.zip_suffix)


@dataclass(frozen=True)
class DependencyProvenancePolicy:
    """Machine-readable source and layout vocabulary."""

    shared_folder_mode: str = "shared-folder"
    github_release_mode: str = "github-release"
    distribution_layout: str = "distribution"
    gradle_install_layout: str = "gradle-install"
    source_launcher_layout: str = "source-launcher"
    environment_layout: str = "environment"
    explicit_executable_layout: str = "explicit-executable"
    managed_install_layout: str = "managed-install"
    shared_folder_display: str = "shared folder (read-only)"
    github_release_display: str = "GitHub release"
    clean_state: str = "clean"
    dirty_state: str = "dirty"
    sbk_local_action: str = "validate and execute only; SBK build is external"
    charts_local_action: str = (
        "execute selected command; launcher owns its runtime"
    )
    sbk_status_action: str = "no build performed"
    charts_status_action: str = "no readiness command started"
    git_command: str = "git"
    git_revision_arguments: tuple[str, ...] = (
        "rev-parse", "--short=12", "HEAD",
    )
    git_status_arguments: tuple[str, ...] = (
        "status", "--porcelain", "--untracked-files=no",
    )


@dataclass(frozen=True)
class EnvironmentPolicy:
    """Environment variable names shared by dependency resolution and runners."""

    sbk_java_home: str = "SBK_JAVA_HOME"
    java_home: str = "JAVA_HOME"
    java_tool_options: str = "JAVA_TOOL_OPTIONS"
    path: str = "PATH"
    git_ssl_no_verify: str = "GIT_SSL_NO_VERIFY"
    git_ssl_ca_info: str = "GIT_SSL_CAINFO"
    pip_cert: str = "PIP_CERT"
    enabled_value: str = "1"
    lifecycle_folder: str = "SBK_ANALYTICS_LIFECYCLE_FOLDER"
    lifecycle_run_id: str = "SBK_ANALYTICS_RUN_ID"
    application_state_home: str = "SBK_ANALYTICS_ENV_HOME"
    xdg_state_home: str = "XDG_STATE_HOME"
    source_root: str = "SBK_ANALYTICS_SOURCE_ROOT"
    downloads_folder: str = "SBK_ANALYTICS_DOWNLOADS_FOLDER"
    legacy_cache_folder: str = "SBK_ANALYTICS_CACHE"
    sbk_local_folder: str = "SBK_LOCAL_FOLDER"
    charts_local_folder: str = "SBK_CHARTS_LOCAL_FOLDER"
    charts_local_executable: str = "SBK_CHARTS_LOCAL_EXECUTABLE"


@dataclass(frozen=True)
class SbkInterfacePolicy:
    """Stable SBK YAML wrapper and lifecycle option names."""

    local_arguments_wrapper: str = "sbkArgs"
    gem_arguments_wrapper: str = "sbkGemArgs"
    nodes_option: str = "nodes"
    seconds_option: str = "seconds"
    class_option: str = "class"
    output_option: str = "out"
    csv_file_option: str = "csvfile"
    csv_logger: str = "CSVLogger"
    gem_user_option: str = "gemuser"
    gem_password_option: str = "gempass"
    gem_port_option: str = "gemport"


@dataclass(frozen=True)
class SbkContractPolicy:
    """Current SBK option vocabulary, validation groups, and migrations."""

    removed_gem_options: tuple[tuple[str, str], ...] = (
        ("copyonlydrivers", "use 'fullcopy: false' for compact driver-scoped provisioning"),
        ("compactruntimecopy", "use 'fullcopy' with the former value inverted"),
        ("compactcopy", "use 'fullcopy' with the former value inverted"),
        ("copy", "runtime content is now provisioned automatically"),
        ("deleteafter", "use 'packagescleanup' to control stale managed packages"),
        ("delete", "invalid managed runtimes are repaired automatically"),
        ("sbkcommand", "SBK-GEM now selects the standard launcher itself"),
        ("sbkdir", "SBK-GEM now receives its application home from its launcher"),
        ("javacopy", "SBK-GEM now provisions or reuses Java automatically"),
        ("javaversion", "configure sbk.jdk.version in sbk-config.env instead"),
    )
    gem_only_options: tuple[str, ...] = (
        "gemuser", "gempass", "hostkeycheck", "knownhosts", "gemport",
        "javadir", "packagescleanup", "fullcopy", "localhost", "sbmport",
        "sbmsleepms", "totalrecords", "totalthroughput",
    )
    boolean_options: tuple[str, ...] = (
        "hostkeycheck", "packagescleanup", "fullcopy",
    )
    positive_integer_options: tuple[str, ...] = (
        "idletimeoutseconds", "gemport", "sbmport", "totalrecords",
    )
    nonnegative_integer_options: tuple[str, ...] = ("sbmsleepms",)
    positive_decimal_options: tuple[str, ...] = ("totalthroughput",)
    mutually_exclusive_options: tuple[tuple[str, str], ...] = (
        ("totalrecords", "records"),
        ("totalrecords", "throughput"),
        ("totalthroughput", "throughput"),
    )
    deprecated_cleanup_option: str = "runtimecleanup"
    cleanup_option: str = "packagescleanup"
    total_records_option: str = "totalrecords"
    total_throughput_option: str = "totalthroughput"


@dataclass(frozen=True)
class ChartsInterfacePolicy:
    """Stable sbk-charts command and runtime resource names."""

    input_option: str = "-i"
    output_option: str = "-o"
    chat_option: str = "-chat"
    working_directory_suffix: str = "-cwd"
    banner_path: tuple[str, ...] = ("src", "main", "banner.txt")


@dataclass(frozen=True)
class CliPolicy:
    """Stable command vocabulary and machine-readable status values."""

    run_command: str = "run"
    dependencies_command: str = "deps"
    configuration_command: str = "config"
    doctor_subcommand: str = "doctor"
    status_subcommand: str = "status"
    initialize_subcommand: str = "init"
    local_profile: str = "local"
    local_config_filename: str = "sbk-config.local.env"
    success_status: str = "ok"
    failed_status: str = "failed"
    error_status: str = "error"
    command_destination: str = "command"
    subcommand_destination: str = "subcommand"

    @property
    def commands(self) -> tuple[str, ...]:
        return (
            self.run_command,
            self.dependencies_command,
            self.configuration_command,
        )

    @property
    def subcommands(self) -> tuple[str, ...]:
        return (
            self.doctor_subcommand,
            self.status_subcommand,
            self.initialize_subcommand,
        )


@dataclass(frozen=True)
class DiagnosticFieldPolicy:
    """Stable JSON field names emitted by CLI diagnostics and run summaries."""

    status: str = "status"
    error: str = "error"
    error_type: str = "error_type"
    exit_code: str = "exit_code"
    signal: str = "signal"
    command: str = "command"
    output: str = "output"
    reason: str = "reason"
    sbk: str = "sbk"
    charts: str = "sbk_charts"
    jdk: str = "jdk"
    source: str = "source"
    home: str = "home"
    executable: str = "executable"
    detected_version: str = "detected_version"
    provenance: str = "provenance"
    downloads_folder: str = "downloads_folder"
    ssl_verify: str = "ssl_verify"
    lifecycle: str = "lifecycle"
    selection: str = "selection"
    configured_local: str = "configured_local"
    configured_executable: str = "configured_executable"
    shared_folder: str = "shared_folder"
    repository_url: str = "repository_url"
    release_tag: str = "release_tag"
    managed_cache: str = "managed_cache"
    cache_complete: str = "cache_complete"
    cache_metadata: str = "cache_metadata"
    successful_instances: str = "successful_instances"
    failed_instances: str = "failed_instances"
    cleanup: str = "cleanup"
    cleanup_policy: str = "policy"
    removed_paths: str = "removed_paths"
    cleanup_before_run: str = "before_run"
    before_run_removed_entries: str = "before_run_removed_entries"
    filesystem_free_bytes_after: str = "filesystem_free_bytes_after"
    valid: str = "valid"
    layout: str = "layout"
    configured_location: str = "configured_location"
    resolved_location: str = "resolved_location"
    revision: str = "revision"
    dirty: str = "dirty"
    sbk_yal: str = "sbk_yal"
    sbk_gem_yal: str = "sbk_gem_yal"
    build_performed: str = "build_performed"
    read_only: str = "read_only"
    install_performed: str = "install_performed"
    sbk_yal_executable: str = "sbk_yal_executable"
    sbk_gem_yal_executable: str = "sbk_gem_yal_executable"
    executable_ready: str = "executable_ready"


@dataclass(frozen=True)
class DisplayPolicy:
    """Shared human-readable output and unit conversion values."""

    section_width: int = 78
    bytes_per_kibibyte: int = 1024
    percentage_scale: float = 100.0
    diagnostic_tail_characters: int = 500
    system_info_tail_characters: int = 120
    logging_verbosity_step: int = 10
    unknown_value: str = "unknown"
    absent_value: str = "none"
    text_encoding: str = "utf-8"


@dataclass(frozen=True)
class NetworkPolicy:
    github_web_url: str = "https://github.com"
    github_api_url: str = (
        "https://api.github.com/repos/{repo}/releases/tags/{tag}"
    )
    github_api_version: str = "2022-11-28"
    release_assets_field: str = "assets"
    release_asset_name_field: str = "name"
    release_asset_url_field: str = "browser_download_url"
    release_asset_digest_field: str = "digest"
    sha256_digest_prefix: str = "sha256:"
    github_metadata_timeout_s: float = 30.0
    artifact_download_timeout_s: float = 120.0
    artifact_download_attempts: int = 6
    download_chunk_bytes: int = 1024 * 1024
    download_progress_interval_s: float = 2.0
    download_retry_cap_s: float = 30.0
    pip_trusted_hosts: tuple[str, ...] = (
        "github.com",
        "pypi.org",
        "files.pythonhosted.org",
        "pypi.python.org",
        "raw.githubusercontent.com",
    )
    pip_module: str = "pip"
    pip_install_subcommand: str = "install"
    pip_trusted_host_option: str = "--trusted-host"
    pip_quiet_option: str = "--quiet"
    pip_upgrade_option: str = "--upgrade"


@dataclass(frozen=True)
class DependencyPolicy:
    default_jdk_version: str = "25"
    default_ssl_verify: bool = False
    warn_version_policy: str = "warn"
    exact_version_policy: str = "exact"
    ignore_version_policy: str = "ignore"
    command_version_timeout_s: float = 20.0
    charts_readiness_timeout_s: float = 60.0
    java_version_timeout_s: float = 10.0
    source_control_timeout_s: float = 5.0
    generic_version_pattern: str = r"(\d+(?:\.\d+)+)"
    python_metadata_script_template: str = (
        "import importlib.metadata as m; print(m.version('{distribution}'))"
    )

    @property
    def default_version_policy(self) -> str:
        return self.warn_version_policy

    @property
    def version_policies(self) -> tuple[str, ...]:
        return (
            self.warn_version_policy,
            self.exact_version_policy,
            self.ignore_version_policy,
        )


@dataclass(frozen=True)
class ProcessPolicy:
    termination_grace_s: float = 3.0
    guard_poll_interval_s: float = 0.05
    guard_pipe_poll_interval_s: float = 0.25
    guard_exit_padding_s: float = 1.0
    guard_force_wait_s: float = 1.0


@dataclass(frozen=True)
class LifecyclePolicy:
    """Durable workload-ownership registry and reconciliation policy."""

    registry_directory: str = "runs"
    linux_state_path: tuple[str, ...] = (".local", "state")
    macos_state_path: tuple[str, ...] = ("Library", "Application Support")
    record_suffix: str = ".json"
    unresolved_suffix: str = ".unresolved"
    temporary_suffix: str = ".tmp"
    schema_version: int = 1
    registry_directory_mode: int = 0o700
    record_mode: int = 0o600
    identity_tolerance_s: float = 0.1
    reconciliation_poll_interval_s: float = 0.05
    local_role: str = "sbk"
    gem_role: str = "sbk-gem"
    charts_role: str = "sbk-charts"
    schema_field: str = "schema"
    run_id_field: str = "run_id"
    controller_pid_field: str = "controller_pid"
    controller_create_time_field: str = "controller_create_time"
    process_pid_field: str = "pid"
    process_create_time_field: str = "process_create_time"
    process_group_field: str = "pgid"
    role_field: str = "role"
    command_field: str = "command"
    metadata_field: str = "metadata"
    created_at_field: str = "created_at"
    registry_field: str = "registry"
    records_field: str = "records"
    active_field: str = "active"
    stale_field: str = "stale"
    unresolved_field: str = "unresolved"
    cleaned_field: str = "cleaned"
    expired_field: str = "expired"
    controller_active_field: str = "controller_active"
    process_active_field: str = "process_active"
    group_active_field: str = "group_active"
    record_field: str = "record"
    error_field: str = "error"
    instance_metadata_field: str = "instance"
    yaml_metadata_field: str = "yaml"
    remote_nodes_metadata_field: str = "remote_nodes"
    remote_owner_metadata_field: str = "remote_cleanup_owner"
    charts_output_metadata_field: str = "output"
    charts_input_count_metadata_field: str = "input_count"
    process_id_attribute: str = "pid"
    process_status_attribute: str = "status"


@dataclass(frozen=True)
class SshPolicy:
    ssh_command: str = "ssh"
    sshpass_command: str = "sshpass"
    sshpass_environment: str = "SSHPASS"
    sshpass_environment_option: str = "-e"
    port_option: str = "-p"
    option_flag: str = "-o"
    batch_mode_option: str = "BatchMode"
    connect_timeout_option: str = "ConnectTimeout"
    remote_shell_command: str = "bash -s"
    enabled_value: str = "yes"
    disabled_value: str = "no"
    default_port: int = 22
    connect_timeout_s: int = 5
    strict_host_key_checking: bool = False
    known_hosts_file: str = os.devnull
    system_info_command_timeout_s: float = 30.0

    @property
    def host_key_arguments(self) -> tuple[str, ...]:
        strict = (
            self.enabled_value
            if self.strict_host_key_checking
            else self.disabled_value
        )
        return (
            self.option_flag, f"StrictHostKeyChecking={strict}",
            self.option_flag, f"UserKnownHostsFile={self.known_hosts_file}",
        )


@dataclass(frozen=True)
class BenchmarkPolicy:
    process_poll_interval_s: float = 0.5
    heartbeat_interval_s: float = 5.0
    gem_native_shutdown_grace_s: float = 30.0
    log_forward_join_s: float = 1.0


@dataclass(frozen=True)
class SystemInfoPolicy:
    linux_platform: str = "Linux"
    macos_platform: str = "Darwin"
    cpu_info_file: str = "/proc/cpuinfo"
    process_cgroup_file: str = "/proc/1/cgroup"
    self_cgroup_file: str = "/proc/self/cgroup"
    docker_environment_file: str = "/.dockerenv"
    kubernetes_service_environment: str = "KUBERNETES_SERVICE_HOST"
    kubernetes_runtime: str = "kubernetes"
    docker_runtime: str = "docker"
    lscpu_command: tuple[str, ...] = ("lscpu",)
    macos_cpu_command: tuple[str, ...] = (
        "sysctl", "-n", "machdep.cpu.brand_string",
    )
    local_command_timeout_s: float = 5.0
    default_sheet_name: str = "system"
    source_column: str = "Source"
    instances_column: str = "Used by instances"
    hostname_column: str = "Hostname"
    operating_system_column: str = "OS"
    operating_system_version_column: str = "OS version"
    architecture_column: str = "Architecture"
    cpu_model_column: str = "CPU model"
    physical_cpus_column: str = "Physical CPUs"
    logical_cpus_column: str = "Logical CPUs"
    cpu_mhz_column: str = "CPU MHz"
    total_ram_column: str = "Total RAM (GiB)"
    available_ram_column: str = "Available RAM (GiB)"
    container_runtime_column: str = "Container runtime"
    container_id_column: str = "Container ID"
    kubernetes_pod_column: str = "K8s Pod"
    kubernetes_namespace_column: str = "K8s Namespace"
    collected_at_column: str = "Collected at"
    status_column: str = "Status"
    source_kind_field: str = "kind"
    source_instances_field: str = "instances"
    source_node_field: str = "node"
    source_user_field: str = "user"
    source_password_field: str = "password"
    source_port_field: str = "port"
    local_source: str = "local"
    remote_source: str = "remote"
    remote_hostname_field: str = "hostname"
    remote_os_field: str = "os"
    remote_os_version_field: str = "os_version"
    remote_architecture_field: str = "arch"
    remote_cpu_model_field: str = "cpu_model"
    remote_physical_cpus_field: str = "physical_cpus"
    remote_logical_cpus_field: str = "logical_cpus"
    remote_cpu_mhz_field: str = "cpu_mhz"
    remote_total_ram_kb_field: str = "total_ram_kb"
    remote_available_ram_kb_field: str = "avail_ram_kb"
    remote_container_runtime_field: str = "container_runtime"
    remote_container_id_field: str = "container_id"
    remote_kubernetes_pod_field: str = "k8s_pod"
    remote_kubernetes_namespace_field: str = "k8s_namespace"
    column_widths: tuple[int, ...] = (
        22, 26, 24, 24, 32, 14, 38, 14, 14, 12, 18, 20, 18, 34, 28, 18,
        22, 22,
    )
    container_id_characters: int = 64

    @property
    def columns(self) -> tuple[str, ...]:
        return (
            self.source_column,
            self.instances_column,
            self.hostname_column,
            self.operating_system_column,
            self.operating_system_version_column,
            self.architecture_column,
            self.cpu_model_column,
            self.physical_cpus_column,
            self.logical_cpus_column,
            self.cpu_mhz_column,
            self.total_ram_column,
            self.available_ram_column,
            self.container_runtime_column,
            self.container_id_column,
            self.kubernetes_pod_column,
            self.kubernetes_namespace_column,
            self.collected_at_column,
            self.status_column,
        )


@dataclass(frozen=True)
class ConfigurationPolicy:
    default_mode: str = "serial"
    valid_modes: tuple[str, ...] = ("serial", "parallel")
    default_workdir: str = os.path.join(tempfile.gettempdir(), APPLICATION.name)
    default_output: str = f"{APPLICATION.name}.xlsx"
    default_ai_model: str = "noai"
    valid_ai_models: tuple[str, ...] = (
        "huggingface", "ollama", "lmstudio", "noai",
    )
    default_cleanup: str = "never"
    cleanup_on_success: str = "on-success"
    default_cleanup_before_run: bool = False
    true_tokens: tuple[str, ...] = ("1", "true", "yes", "on")
    false_tokens: tuple[str, ...] = ("0", "false", "no", "off")
    mode_keys: tuple[str, ...] = ("mode",)
    workdir_keys: tuple[str, ...] = ("workdir", "work_dir", "work-dir")
    cleanup_keys: tuple[str, ...] = ("cleanup",)
    cleanup_before_run_keys: tuple[str, ...] = (
        "cleanup_before_run", "cleanup-before-run",
    )
    sbk_group_keys: tuple[str, ...] = ("sbk", "sbk_params", "sbk-params")
    classes_keys: tuple[str, ...] = ("classes", "class_list", "class-list")
    class_params_keys: tuple[str, ...] = (
        "class_params", "class-params", "classparams",
    )
    charts_group_keys: tuple[str, ...] = (
        "sbk-charts", "sbk_charts", "sbkcharts",
    )
    charts_legacy_keys: tuple[str, ...] = (
        "output", "ai_model", "ai-model", "ai_params", "ai-params",
        "chat", "chat_mode",
    )
    charts_forbidden_input_keys: tuple[str, ...] = (
        "ifiles", "ifile", "input", "inputs", "-i", "i",
    )
    charts_output_keys: tuple[str, ...] = (
        "output", "ofile", "output_excel", "excel",
    )
    charts_ai_model_keys: tuple[str, ...] = (
        "ai_model", "ai-model", "ai",
    )
    charts_ai_params_keys: tuple[str, ...] = (
        "ai_params", "ai-params", "ai_model_params",
    )
    charts_chat_keys: tuple[str, ...] = ("chat", "chat_mode", "chat-mode")
    charts_use_files_keys: tuple[str, ...] = (
        "use_files", "use-files", "usefiles",
    )
    instance_class_keys: tuple[str, ...] = ("class", "class_name")
    instance_name_key: str = "name"

    @property
    def valid_cleanup(self) -> tuple[str, ...]:
        return (self.default_cleanup, self.cleanup_on_success)


@dataclass(frozen=True)
class PropertiesPolicy:
    """Accepted canonical and legacy keys in sbk-config.env files."""

    sbk_url_keys: tuple[str, ...] = ("sbk.url", "sbk_url")
    charts_url_keys: tuple[str, ...] = (
        "sbk.charts.url", "sbk_charts_url", "sbkcharts.url",
    )
    jdk_version_keys: tuple[str, ...] = (
        "sbk.jdk.version", "sbk_jdk_version", "jdk.version", "jdk_version",
    )
    downloads_folder_keys: tuple[str, ...] = (
        "downloads.folder", "downloads_folder",
    )
    sbk_local_folder_keys: tuple[str, ...] = (
        "sbk.local.folder", "sbk_local_folder",
    )
    charts_local_folder_keys: tuple[str, ...] = (
        "sbk.charts.local.folder", "sbk_charts_local_folder",
        "sbkcharts.local.folder",
    )
    charts_local_executable_keys: tuple[str, ...] = (
        "sbk.charts.local.executable", "sbk_charts_local_executable",
        "sbkcharts.local.executable",
    )
    charts_sha256_keys: tuple[str, ...] = (
        "sbk.charts.sha256", "sbk_charts_sha256", "sbkcharts.sha256",
    )
    jdk_folder_keys: tuple[str, ...] = (
        "sbk.jdk.folder", "sbk_jdk_folder", "jdk.folder", "jdk_folder",
    )
    ssl_verify_keys: tuple[str, ...] = (
        "ssl.verify", "ssl_verify", "verify", "verify.ssl",
    )
    ssl_ca_bundle_keys: tuple[str, ...] = (
        "ssl.ca.bundle", "ssl_ca_bundle",
    )
    sbk_version_keys: tuple[str, ...] = ("sbk.version", "sbk_version")
    charts_version_keys: tuple[str, ...] = (
        "sbk.charts.version", "sbk_charts_version", "sbkcharts.version",
    )
    sbk_version_policy_keys: tuple[str, ...] = ("sbk.version.policy",)
    charts_version_policy_keys: tuple[str, ...] = (
        "sbk.charts.version.policy",
    )


@dataclass(frozen=True)
class ExitCodePolicy:
    success: int = 0
    no_usable_csv: int = 2
    missing_output: int = 3
    system_info_failure: int = 4
    handled_error: int = 5
    signal_base: int = 128


@dataclass(frozen=True)
class RuntimePolicy:
    cache: CachePolicy = CachePolicy()
    cache_metadata: CacheMetadataPolicy = CacheMetadataPolicy()
    dependency_layout: DependencyLayoutPolicy = DependencyLayoutPolicy()
    archives: ArchivePolicy = ArchivePolicy()
    provenance: DependencyProvenancePolicy = DependencyProvenancePolicy()
    environment: EnvironmentPolicy = EnvironmentPolicy()
    sbk_interface: SbkInterfacePolicy = SbkInterfacePolicy()
    sbk_contract: SbkContractPolicy = SbkContractPolicy()
    charts_interface: ChartsInterfacePolicy = ChartsInterfacePolicy()
    cli: CliPolicy = CliPolicy()
    diagnostics: DiagnosticFieldPolicy = DiagnosticFieldPolicy()
    display: DisplayPolicy = DisplayPolicy()
    network: NetworkPolicy = NetworkPolicy()
    dependencies: DependencyPolicy = DependencyPolicy()
    processes: ProcessPolicy = ProcessPolicy()
    lifecycle: LifecyclePolicy = LifecyclePolicy()
    ssh: SshPolicy = SshPolicy()
    benchmarks: BenchmarkPolicy = BenchmarkPolicy()
    system_info: SystemInfoPolicy = SystemInfoPolicy()
    configuration: ConfigurationPolicy = ConfigurationPolicy()
    properties: PropertiesPolicy = PropertiesPolicy()
    exit_codes: ExitCodePolicy = ExitCodePolicy()


RUNTIME_POLICY = RuntimePolicy()
