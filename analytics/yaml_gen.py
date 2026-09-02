#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Generate per-instance YAML files for sbk-yal / sbk-gem-yal."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Instance
from .policy import RUNTIME_POLICY


SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface


def _normalise_nodes(value: Any) -> Any:
    """sbk-gem-yal expects a comma-separated string for `nodes`."""
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return value


def generate_instance_yaml(
    inst: Instance,
    out_dir: Path,
    csv_path: Path,
) -> Path:
    """Write a YAML file for one SBK benchmark instance and return its path.

    The supported SBK YML loaders expect arguments under a top-level key:
        sbkArgs:     for sbk-yal
        sbkGemArgs:  for sbk-gem-yal (i.e. when ``nodes`` is present)

    The merged params from the Instance are written under that wrapper, with
    ``class``, a CSV-capable logger, and ``csvfile`` are forced so each
    instance produces its own CSV. SBK-YAL uses ``CSVLogger``; SBK-GEM-YAL
    uses ``GemPrometheusLogger``, the SBK 10.6 GEM logger that persists CSV.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    params: dict[str, Any] = dict(inst.params)
    params[SBK_INTERFACE_POLICY.class_option] = inst.class_name
    logger = (
        SBK_INTERFACE_POLICY.gem_csv_logger
        if inst.uses_gem
        else SBK_INTERFACE_POLICY.csv_logger
    )
    params[SBK_INTERFACE_POLICY.output_option] = logger
    params[SBK_INTERFACE_POLICY.csv_file_option] = str(csv_path)

    wrapper_key = SBK_INTERFACE_POLICY.local_arguments_wrapper
    if inst.uses_gem:
        nodes_option = SBK_INTERFACE_POLICY.nodes_option
        params[nodes_option] = _normalise_nodes(params[nodes_option])
        wrapper_key = SBK_INTERFACE_POLICY.gem_arguments_wrapper

    yml_path = out_dir / f"sbk-{inst.name}.yml"
    with yml_path.open("w") as f:
        yaml.safe_dump(
            {wrapper_key: params},
            f,
            sort_keys=False,
            default_flow_style=False,
        )
    return yml_path
