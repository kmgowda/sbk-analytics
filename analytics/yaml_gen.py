"""Generate per-instance YAML files for sbk-yal / sbk-gem-yal."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import Instance


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

    SBK 9.0's YML loader expects all arguments wrapped under a top-level key:
        sbkArgs:     for sbk-yal
        sbkGemArgs:  for sbk-gem-yal (i.e. when ``nodes`` is present)

    The merged params from the Instance are written under that wrapper, with
    ``class``, ``out=CSVLogger``, and ``csvfile`` forced so each instance
    produces its own CSV.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    params: dict[str, Any] = dict(inst.params)
    params["class"] = inst.class_name
    params["out"] = "CSVLogger"
    params["csvfile"] = str(csv_path)

    wrapper_key = "sbkArgs"
    if "nodes" in params:
        params["nodes"] = _normalise_nodes(params["nodes"])
        wrapper_key = "sbkGemArgs"

    yml_path = out_dir / f"sbk-{inst.name}.yml"
    with yml_path.open("w") as f:
        yaml.safe_dump(
            {wrapper_key: params},
            f,
            sort_keys=False,
            default_flow_style=False,
        )
    return yml_path
