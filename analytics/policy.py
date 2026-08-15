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
"""
from __future__ import annotations

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
)

SBK_CHARTS_ARTIFACT = ArtifactMetadata(
    key="sbk-charts",
    display_name="sbk-charts",
    distribution_name="sbk-charts",
    repository_url="https://github.com/kmgowda/sbk-charts",
    cache_namespace="sbk-charts",
    primary_executable="sbk-charts",
    repository_slug="kmgowda/sbk-charts",
)

JDK_ARTIFACT = ArtifactMetadata(
    key="jdk",
    display_name="JDK",
    distribution_name="Temurin JDK",
    repository_url=(
        "https://api.adoptium.net/v3/binary/latest/{version}/ga/"
        "{os}/{arch}/jdk/hotspot/normal/eclipse"
    ),
    cache_namespace="jdk",
    primary_executable="java",
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


@dataclass(frozen=True)
class NetworkPolicy:
    github_web_url: str = "https://github.com"
    github_api_url: str = (
        "https://api.github.com/repos/{repo}/releases/tags/{tag}"
    )
    github_api_version: str = "2022-11-28"
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
class SshPolicy:
    default_port: int = 22
    connect_timeout_s: int = 5
    strict_host_key_checking: bool = False
    known_hosts_file: str = "/dev/null"
    remote_kill_command_timeout_s: float = 10.0
    system_info_command_timeout_s: float = 30.0

    @property
    def host_key_arguments(self) -> tuple[str, ...]:
        strict = "yes" if self.strict_host_key_checking else "no"
        return (
            "-o", f"StrictHostKeyChecking={strict}",
            "-o", f"UserKnownHostsFile={self.known_hosts_file}",
        )


@dataclass(frozen=True)
class BenchmarkPolicy:
    remote_kill_grace_s: float = 10.0
    local_kill_grace_s: float = 15.0
    watchdog_poll_interval_s: float = 0.5
    heartbeat_interval_s: float = 5.0
    remote_kill_join_timeout_s: float = 15.0
    remote_before_local_join_s: float = 5.0
    local_terminate_wait_s: float = 2.0
    log_forward_join_s: float = 1.0
    remote_process_pattern: str = "io.sbk.main"
    remote_kill_signal: int = 9


@dataclass(frozen=True)
class SystemInfoPolicy:
    local_command_timeout_s: float = 5.0
    default_sheet_name: str = "system"


@dataclass(frozen=True)
class ConfigurationPolicy:
    default_mode: str = "serial"
    valid_modes: tuple[str, ...] = ("serial", "parallel")
    default_workdir: str = f"/tmp/{APPLICATION.name}"
    default_output: str = f"{APPLICATION.name}.xlsx"
    default_ai_model: str = "noai"
    valid_ai_models: tuple[str, ...] = (
        "huggingface", "ollama", "lmstudio", "noai",
    )
    default_cleanup: str = "never"
    valid_cleanup: tuple[str, ...] = ("never", "on-success")
    true_tokens: tuple[str, ...] = ("1", "true", "yes", "on")
    false_tokens: tuple[str, ...] = ("0", "false", "no", "off")


@dataclass(frozen=True)
class ExitCodePolicy:
    success: int = 0
    no_usable_csv: int = 2
    missing_output: int = 3
    system_info_failure: int = 4
    handled_error: int = 5


@dataclass(frozen=True)
class RuntimePolicy:
    cache: CachePolicy = CachePolicy()
    network: NetworkPolicy = NetworkPolicy()
    dependencies: DependencyPolicy = DependencyPolicy()
    processes: ProcessPolicy = ProcessPolicy()
    ssh: SshPolicy = SshPolicy()
    benchmarks: BenchmarkPolicy = BenchmarkPolicy()
    system_info: SystemInfoPolicy = SystemInfoPolicy()
    configuration: ConfigurationPolicy = ConfigurationPolicy()
    exit_codes: ExitCodePolicy = ExitCodePolicy()


RUNTIME_POLICY = RuntimePolicy()
