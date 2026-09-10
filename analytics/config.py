#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
"""Parse the input YML driving the orchestration.

The YML contains the following groups (see README):

    mode:             serial | parallel
    sbk:              shared SBK / SBK-GEM-YAL defaults
    benchmarks:       named instance -> SBK class -> parameter mappings
    class_params:     (optional) per-class defaults
    cleanup:          never | on-success; only File-driver file/fname paths
                      contained by workdir are eligible for removal
    cleanup_before_run: false | true; empty workdir before starting SBK
    sbk-charts:       options for the sbk-charts invocation
      output:         output xlsx file
      ai_model:       huggingface | ollama | lmstudio | noai
      ai_params:      params passed to the AI sub-command
      chat:           true | false (sbk-charts -chat mode)
      # The input CSV files for sbk-charts are ALWAYS the unique CSVs produced
      # by the SBK instances above. They are not set in the YAML.

The legacy top-level keys ``classes``, ``output``, ``ai_model``, ``ai_params``
and ``chat`` are still accepted for backwards compatibility but emit a
deprecation warning.

The canonical format identifies both the analytics instance and SBK class by
their mapping keys::

    benchmarks:
      file_write:
        file:
          writers: 1
          fname: /tmp/a
      rocksdb_write:
        rocksdb:
          writers: 1
          rfile: /tmp/rdb

The first mapping key is the analytics-owned instance name. Its single child
key is translated into the SBK ``class`` parameter. Values nested under that
class are passed to SBK without downstream option validation. Shared ``sbk:``
values are merged first and nested instance values override them.

The former sequence format remains readable for compatibility but emits a
deprecation warning::

    benchmarks:
      - class: file
        writers: 1
        fname: /tmp/a
      - class: file
        readers: 1
        fname: /tmp/a
      - class: file
        writers: 1
        size: 1000
        fname: /tmp/b
      - class: rocksdb
        writers: 1
        rfile: /tmp/rdb

Legacy sequence entries may set ``name:`` to override their generated label.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode

from .policy import RUNTIME_POLICY


CONFIGURATION_POLICY = RUNTIME_POLICY.configuration
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface
log = logging.getLogger(__name__)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects keys which would otherwise be replaced."""

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise ValueError(
                    f"YAML mapping key must be hashable, got {key!r}"
                ) from error
            if duplicate:
                raise ValueError(f"duplicate YAML key {key!r}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass
class Instance:
    """One SBK benchmark invocation."""
    name: str            # unique label, also used for YAML/CSV filenames
    class_name: str      # SBK storage class (e.g. 'file', 'rocksdb')
    params: dict[str, Any]  # already merged with shared sbk-params

    @property
    def uses_gem(self) -> bool:
        """Whether this instance requires the distributed GEM runner."""
        return _has_value(
            self.params.get(SBK_INTERFACE_POLICY.nodes_option)
        )


@dataclass
class OrchestratorConfig:
    mode: str = CONFIGURATION_POLICY.default_mode
    workdir: str = CONFIGURATION_POLICY.default_workdir
    sbk_params: dict[str, Any] = field(default_factory=dict)
    instances: list[Instance] = field(default_factory=list)
    output: str = CONFIGURATION_POLICY.default_output
    ai_model: str = CONFIGURATION_POLICY.default_ai_model
    ai_params: dict[str, Any] = field(default_factory=dict)
    chat: bool = False
    # Additional CSV files supplied by the user (already-available benchmark
    # results) to be passed to sbk-charts alongside the freshly-generated
    # instance CSVs.
    use_files: list[str] = field(default_factory=list)
    # Deliberately narrow: on-success cleanup handles only class=file data
    # selected by file/fname, and cli.py enforces workdir containment.
    cleanup: str = CONFIGURATION_POLICY.default_cleanup
    # Destructive pre-run cleanup is opt-in and applies to every direct entry
    # below workdir. cli.py validates protected paths before removing anything.
    cleanup_before_run: bool = CONFIGURATION_POLICY.default_cleanup_before_run

    @property
    def uses_gem(self) -> bool:
        """True if SBK-GEM-YAL should be used (i.e. 'nodes' is set in shared
        sbk params or in *any* instance's params)."""
        return any(instance.uses_gem for instance in self.instances)


def _has_value(value: Any) -> bool:
    """Return whether a configuration value is meaningfully populated."""
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return len(value) > 0
    return bool(str(value).strip())


def _parse_bool(value: Any, field_name: str) -> bool:
    """Parse a YAML boolean without treating every non-empty string as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalised = value.strip().lower()
        if normalised in CONFIGURATION_POLICY.true_tokens:
            return True
        if normalised in CONFIGURATION_POLICY.false_tokens:
            return False
    raise ValueError(
        f"'{field_name}' must be a boolean "
        f"(true/false, yes/no, on/off, or 1/0), got {value!r}"
    )


def _first(d: dict, *names: str, default=None):
    for n in names:
        if n in d:
            return d[n]
    return default


def _validate_orchestrator_keys(raw: dict[Any, Any], path: Path) -> None:
    """Reject keys outside the top-level schema owned by sbk-analytics.

    Values inside ``sbk:``, ``class_params:``, benchmark entries, and
    ``sbk-charts.ai_params`` belong to the independently released downstream
    applications and deliberately are not validated here.
    """
    allowed = set(CONFIGURATION_POLICY.orchestrator_top_level_keys)
    unknown = [
        key for key in raw
        if not isinstance(key, str) or key not in allowed
    ]
    if unknown:
        rendered = ", ".join(repr(key) for key in unknown)
        noun = "key" if len(unknown) == 1 else "keys"
        raise ValueError(
            f"{path}: unknown sbk-analytics top-level {noun}: {rendered}"
        )


def load_config(path: str | Path) -> OrchestratorConfig:
    p = Path(path)
    raw = yaml.load(p.read_text(), Loader=_UniqueKeySafeLoader) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected top-level mapping, got {type(raw).__name__}")
    _validate_orchestrator_keys(raw, p)

    mode = str(
        _first(
            raw, *CONFIGURATION_POLICY.mode_keys,
            default=CONFIGURATION_POLICY.default_mode,
        )
    ).strip().lower() or CONFIGURATION_POLICY.default_mode
    if mode not in CONFIGURATION_POLICY.valid_modes:
        raise ValueError(
            f"mode must be one of {CONFIGURATION_POLICY.valid_modes}, got {mode!r}"
        )

    workdir = str(
        _first(
            raw, *CONFIGURATION_POLICY.workdir_keys,
            default=CONFIGURATION_POLICY.default_workdir,
        )
    ).strip() or CONFIGURATION_POLICY.default_workdir
    cleanup = str(
        _first(
            raw, *CONFIGURATION_POLICY.cleanup_keys,
            default=CONFIGURATION_POLICY.default_cleanup,
        )
    ).strip().lower()
    if cleanup not in CONFIGURATION_POLICY.valid_cleanup:
        raise ValueError(
            f"cleanup must be one of {CONFIGURATION_POLICY.valid_cleanup}, "
            f"got {cleanup!r}"
        )
    cleanup_before_run = _parse_bool(
        _first(
            raw,
            *CONFIGURATION_POLICY.cleanup_before_run_keys,
            default=CONFIGURATION_POLICY.default_cleanup_before_run,
        ),
        CONFIGURATION_POLICY.cleanup_before_run_keys[0],
    )

    sbk_params = _first(
        raw, *CONFIGURATION_POLICY.sbk_group_keys, default={}
    ) or {}
    if not isinstance(sbk_params, dict):
        raise ValueError("'sbk' must be a mapping of SBK parameters")

    canonical_keys = tuple(
        key for key in CONFIGURATION_POLICY.benchmarks_keys if key in raw
    )
    legacy_keys = tuple(
        key for key in CONFIGURATION_POLICY.legacy_classes_keys if key in raw
    )
    if canonical_keys and legacy_keys:
        raise ValueError(
            "use 'benchmarks' only; legacy 'classes' cannot be combined "
            "with the canonical benchmark list"
        )
    if legacy_keys:
        log.warning(
            "top-level '%s' is deprecated; rename it to 'benchmarks'",
            legacy_keys[0],
        )
        benchmark_entries = _first(
            raw, *CONFIGURATION_POLICY.legacy_classes_keys, default=[]
        ) or []
    else:
        benchmark_entries = _first(
            raw, *CONFIGURATION_POLICY.benchmarks_keys, default=[]
        ) or []
    if isinstance(benchmark_entries, str):
        benchmark_entries = [
            entry.strip() for entry in benchmark_entries.split(",")
            if entry.strip()
        ]
    if not benchmark_entries:
        raise ValueError("'benchmarks' must list at least one benchmark")
    if not isinstance(benchmark_entries, dict):
        log.warning(
            "the benchmark sequence format is deprecated; use "
            "'benchmarks: <instance>: <class>: <parameters>'"
        )

    class_params = (
        _first(raw, *CONFIGURATION_POLICY.class_params_keys, default={}) or {}
    )
    if not isinstance(class_params, dict):
        raise ValueError("'class_params' must be a mapping of class -> params")

    instances = _build_instances(benchmark_entries, class_params, sbk_params)

    output, ai_model, ai_params, chat, use_files = _parse_sbk_charts_group(raw)

    return OrchestratorConfig(
        mode=mode,
        workdir=workdir,
        sbk_params=dict(sbk_params),
        instances=instances,
        output=output,
        ai_model=ai_model,
        ai_params=dict(ai_params),
        chat=chat,
        use_files=list(use_files),
        cleanup=cleanup,
        cleanup_before_run=cleanup_before_run,
    )


def _parse_sbk_charts_group(
    raw: dict,
) -> tuple[str, str, dict[str, Any], bool, list[str]]:
    """Extract the sbk-charts options from the YAML.

    Canonical location is the ``sbk-charts:`` (or ``sbk_charts:``) group::

        sbk-charts:
          output: results.xlsx
          ai_model: noai
          ai_params: {}
          chat: false
          use_files:                     # optional; existing CSV files to
            - /data/baseline-kafka.csv   # combine with the freshly-generated
            - /data/baseline-pulsar.csv  # SBK-instance CSVs

    For backwards compatibility, the top-level keys ``output``, ``ai_model``,
    ``ai_params`` and ``chat`` are still accepted (with a deprecation warning).
    The orchestrator-managed CSV inputs (one per SBK instance) are always
    fed to sbk-charts; ``use_files`` adds to them.
    """
    group = _first(
        raw, *CONFIGURATION_POLICY.charts_group_keys, default=None
    )

    legacy_keys = CONFIGURATION_POLICY.charts_legacy_keys
    has_legacy = any(k in raw for k in legacy_keys)

    if group is not None and not isinstance(group, dict):
        raise ValueError("'sbk-charts' must be a mapping")

    if group is None and has_legacy:
        log.warning(
            "deprecated: sbk-charts options at the YAML top level "
            "(output/ai_model/ai_params/chat); move them under a 'sbk-charts:' group"
        )
        group = {}
        for src in legacy_keys:
            if src in raw:
                group[src] = raw[src]

    group = group or {}

    # reject input-csv specifications -- those are managed by the orchestrator
    forbidden = CONFIGURATION_POLICY.charts_forbidden_input_keys
    for k in forbidden:
        if k in group:
            raise ValueError(
                f"sbk-charts.{k}: do not set; sbk-charts inputs are always "
                f"the CSV files produced by the configured SBK instances"
            )

    output = str(
        _first(
            group, *CONFIGURATION_POLICY.charts_output_keys,
            default=CONFIGURATION_POLICY.default_output,
        )
    )

    ai_model = str(
        _first(
            group, *CONFIGURATION_POLICY.charts_ai_model_keys,
            default=CONFIGURATION_POLICY.default_ai_model,
        )
    ).strip().lower()
    ai_params = _first(
        group, *CONFIGURATION_POLICY.charts_ai_params_keys, default={}
    ) or {}
    if not isinstance(ai_params, dict):
        raise ValueError("'sbk-charts.ai_params' must be a mapping")

    chat = _parse_bool(
        _first(group, *CONFIGURATION_POLICY.charts_chat_keys, default=False),
        "sbk-charts.chat",
    )

    use_files_raw = _first(
        group, *CONFIGURATION_POLICY.charts_use_files_keys, default=None
    )
    if use_files_raw is None:
        use_files: list[str] = []
    elif isinstance(use_files_raw, str):
        use_files = [s.strip() for s in use_files_raw.split(",") if s.strip()]
    elif isinstance(use_files_raw, (list, tuple)):
        use_files = []
        for entry in use_files_raw:
            if entry is None:
                continue
            s = str(entry).strip()
            if s:
                use_files.append(s)
    else:
        raise ValueError(
            "'sbk-charts.use_files' must be a list of CSV file paths "
            f"(or a comma-separated string); got {type(use_files_raw).__name__}"
        )

    return output, ai_model, dict(ai_params), chat, use_files


def _sanitise_name(name: str) -> str:
    """Make a name safe to use in a filename."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)


def _build_instances(
    benchmarks: list | dict[Any, Any],
    class_params: dict[str, dict[str, Any]],
    sbk_params: dict[str, Any],
) -> list[Instance]:
    """Normalise the two declaration styles into a list of Instance objects.

    Canonical entries have the hierarchy ``instance -> class -> parameters``.
    Legacy list entries may be a class string or a mapping containing
    ``class`` and optional ``name`` keys.
    """
    counters: dict[str, int] = {}

    def _unique(label: str) -> str:
        n = counters.get(label, 0)
        counters[label] = n + 1
        return label if n == 0 else f"{label}-{n + 1}"

    if isinstance(benchmarks, dict):
        out: list[Instance] = []
        for instance_name, class_group in benchmarks.items():
            context = f"benchmarks[{instance_name!r}]"
            if (
                not isinstance(instance_name, str)
                or not instance_name.strip()
                or not any(character.isalnum() for character in instance_name)
            ):
                raise ValueError(
                    "benchmark instance names must be non-empty strings "
                    "containing a letter or number"
                )
            if not isinstance(class_group, dict) or len(class_group) != 1:
                raise ValueError(
                    f"{context}: expected exactly one SBK class mapping"
                )
            class_name, instance_params = next(iter(class_group.items()))
            if not isinstance(class_name, str) or not class_name.strip():
                raise ValueError(
                    f"{context}: class name must be a non-empty string"
                )
            if not isinstance(instance_params, dict):
                raise ValueError(
                    f"{context}.{class_name}: class parameters must be a mapping"
                )
            class_name = class_name.strip()
            params = dict(sbk_params)
            params.update(class_params.get(class_name, {}) or {})
            params.update(instance_params)
            out.append(
                Instance(
                    name=_sanitise_name(instance_name.strip()),
                    class_name=class_name,
                    params=params,
                )
            )
        return _validate_unique_instance_names(out)

    if not isinstance(benchmarks, list):
        raise ValueError(
            "'benchmarks' must be a mapping of named benchmark instances"
        )

    out = []
    for idx, entry in enumerate(benchmarks):
        if isinstance(entry, str):
            class_name = entry.strip()
            if not class_name:
                raise ValueError(f"benchmarks[{idx}]: empty class name")
            params = dict(sbk_params)
            params.update(class_params.get(class_name, {}) or {})
            name = _unique(_sanitise_name(class_name))
        elif isinstance(entry, dict):
            class_name = next(
                (
                    entry.get(key)
                    for key in CONFIGURATION_POLICY.instance_class_keys
                    if entry.get(key)
                ),
                None,
            )
            if not class_name:
                raise ValueError(
                    f"benchmarks[{idx}]: dict entry must have a 'class:' key"
                )
            class_name = str(class_name).strip()
            # explicit name overrides auto-numbering
            explicit_name = entry.get(CONFIGURATION_POLICY.instance_name_key)
            params = dict(sbk_params)
            # also apply class_params[class] as a base layer if provided
            params.update(class_params.get(class_name, {}) or {})
            # then the entry's own params (everything except 'class'/'name')
            for k, v in entry.items():
                if k in (
                    *CONFIGURATION_POLICY.instance_class_keys,
                    CONFIGURATION_POLICY.instance_name_key,
                ):
                    continue
                params[k] = v
            name = (
                _sanitise_name(str(explicit_name))
                if explicit_name
                else _unique(_sanitise_name(class_name))
            )
        else:
            raise ValueError(
                f"benchmarks[{idx}]: expected string or mapping, got {type(entry).__name__}"
            )
        out.append(Instance(name=name, class_name=class_name, params=params))

    return _validate_unique_instance_names(out)


def _validate_unique_instance_names(instances: list[Instance]) -> list[Instance]:
    """Reject names that collide after filename-safe normalization."""
    seen: set[str] = set()
    for inst in instances:
        if not inst.name:
            raise ValueError(
                "benchmark instance names must contain a letter or number"
            )
        if inst.name in seen:
            raise ValueError(
                f"duplicate instance name {inst.name!r}; use unique benchmark "
                "section names"
            )
        seen.add(inst.name)
    return instances
