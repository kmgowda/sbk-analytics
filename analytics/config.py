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
    classes:          list of benchmark instance entries
    class_params:     (optional) per-class defaults
    cleanup:          never | on-success; only File-driver file/fname paths
                      contained by workdir are eligible for removal
    sbk-charts:       options for the sbk-charts invocation
      output:         output xlsx file
      ai_model:       huggingface | ollama | lmstudio | noai
      ai_params:      params passed to the AI sub-command
      chat:           true | false (sbk-charts -chat mode)
      # The input CSV files for sbk-charts are ALWAYS the unique CSVs produced
      # by the SBK instances above. They are not set in the YAML.

The legacy top-level keys ``output``, ``ai_model``, ``ai_params`` and ``chat``
are still accepted for backwards compatibility but emit a deprecation warning.

Two styles are supported for declaring the benchmark instances:

Style A - one instance per class (legacy)::

    classes: [file, hdfs]
    class_params:
      file: {fname: /tmp/sbk-test, writers: 1}
      hdfs: {uri: hdfs://localhost:9000, writers: 1}

Style B - multiple instances allowed per class, each with its own params::

    classes:
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

Each list entry becomes one SBK invocation with its own intermediate YAML and
CSV. Entry-level params are merged on top of the shared ``sbk:`` block.

Optionally each entry may set ``name:`` to override the auto-generated label
(used for the YAML/CSV filenames and the summary table); otherwise the name is
``<class>`` for the first occurrence and ``<class>-<n>`` for subsequent ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .policy import RUNTIME_POLICY
from .sbk_contract import normalize_sbk_params


CONFIGURATION_POLICY = RUNTIME_POLICY.configuration
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface


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


def load_config(path: str | Path) -> OrchestratorConfig:
    p = Path(path)
    raw = yaml.safe_load(p.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: expected top-level mapping, got {type(raw).__name__}")

    mode = str(
        _first(raw, "mode", default=CONFIGURATION_POLICY.default_mode)
    ).strip().lower() or CONFIGURATION_POLICY.default_mode
    if mode not in CONFIGURATION_POLICY.valid_modes:
        raise ValueError(
            f"mode must be one of {CONFIGURATION_POLICY.valid_modes}, got {mode!r}"
        )

    workdir = str(
        _first(
            raw, "workdir", "work_dir", "work-dir",
            default=CONFIGURATION_POLICY.default_workdir,
        )
    ).strip() or CONFIGURATION_POLICY.default_workdir
    cleanup = str(
        _first(raw, "cleanup", default=CONFIGURATION_POLICY.default_cleanup)
    ).strip().lower()
    if cleanup not in CONFIGURATION_POLICY.valid_cleanup:
        raise ValueError(
            f"cleanup must be one of {CONFIGURATION_POLICY.valid_cleanup}, "
            f"got {cleanup!r}"
        )

    sbk_params = _first(raw, "sbk", "sbk_params", "sbk-params", default={}) or {}
    if not isinstance(sbk_params, dict):
        raise ValueError("'sbk' must be a mapping of SBK parameters")

    classes = _first(raw, "classes", "class_list", "class-list", default=[]) or []
    if isinstance(classes, str):
        classes = [c.strip() for c in classes.split(",") if c.strip()]
    if not classes:
        raise ValueError("'classes' must list at least one storage class")

    class_params = (
        _first(raw, "class_params", "class-params", "classparams", default={}) or {}
    )
    if not isinstance(class_params, dict):
        raise ValueError("'class_params' must be a mapping of class -> params")

    instances = _build_instances(classes, class_params, sbk_params)

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
    import logging
    _log = logging.getLogger(__name__)

    group = _first(raw, "sbk-charts", "sbk_charts", "sbkcharts", default=None)

    legacy_keys = ("output", "ai_model", "ai-model", "ai_params", "ai-params",
                   "chat", "chat_mode")
    has_legacy = any(k in raw for k in legacy_keys)

    if group is not None and not isinstance(group, dict):
        raise ValueError("'sbk-charts' must be a mapping")

    if group is None and has_legacy:
        _log.warning(
            "deprecated: sbk-charts options at the YAML top level "
            "(output/ai_model/ai_params/chat); move them under a 'sbk-charts:' group"
        )
        group = {}
        for src in legacy_keys:
            if src in raw:
                group[src] = raw[src]

    group = group or {}

    # reject input-csv specifications -- those are managed by the orchestrator
    forbidden = ("ifiles", "ifile", "input", "inputs", "-i", "i")
    for k in forbidden:
        if k in group:
            raise ValueError(
                f"sbk-charts.{k}: do not set; sbk-charts inputs are always "
                f"the CSV files produced by the configured SBK instances"
            )

    output = str(
        _first(group, "output", "ofile", "output_excel", "excel",
               default=CONFIGURATION_POLICY.default_output)
    )

    ai_model = str(
        _first(
            group, "ai_model", "ai-model", "ai",
            default=CONFIGURATION_POLICY.default_ai_model,
        )
    ).strip().lower()
    if ai_model not in CONFIGURATION_POLICY.valid_ai_models:
        raise ValueError(
            "sbk-charts.ai_model must be one of "
            f"{CONFIGURATION_POLICY.valid_ai_models}, got {ai_model!r}"
        )

    ai_params = _first(group, "ai_params", "ai-params", "ai_model_params", default={}) or {}
    if not isinstance(ai_params, dict):
        raise ValueError("'sbk-charts.ai_params' must be a mapping")

    chat = _parse_bool(
        _first(group, "chat", "chat_mode", "chat-mode", default=False),
        "sbk-charts.chat",
    )

    use_files_raw = _first(group, "use_files", "use-files", "usefiles", default=None)
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
    classes: list,
    class_params: dict[str, dict[str, Any]],
    sbk_params: dict[str, Any],
) -> list[Instance]:
    """Normalise the two declaration styles into a list of Instance objects.

    Entries in `classes` may be:
      - a string (class name): merged with class_params[class] if present
      - a mapping with at least 'class:' (or 'name:') and arbitrary SBK params
    """
    counters: dict[str, int] = {}

    def _unique(label: str) -> str:
        n = counters.get(label, 0)
        counters[label] = n + 1
        return label if n == 0 else f"{label}-{n + 1}"

    out: list[Instance] = []
    for idx, entry in enumerate(classes):
        if isinstance(entry, str):
            class_name = entry.strip()
            if not class_name:
                raise ValueError(f"classes[{idx}]: empty class name")
            params = dict(sbk_params)
            params.update(class_params.get(class_name, {}) or {})
            name = _unique(_sanitise_name(class_name))
        elif isinstance(entry, dict):
            class_name = entry.get("class") or entry.get("class_name")
            if not class_name:
                raise ValueError(
                    f"classes[{idx}]: dict entry must have a 'class:' key"
                )
            class_name = str(class_name).strip()
            # explicit name overrides auto-numbering
            explicit_name = entry.get("name")
            params = dict(sbk_params)
            # also apply class_params[class] as a base layer if provided
            params.update(class_params.get(class_name, {}) or {})
            # then the entry's own params (everything except 'class'/'name')
            for k, v in entry.items():
                if k in ("class", "class_name", "name"):
                    continue
                params[k] = v
            name = (
                _sanitise_name(str(explicit_name))
                if explicit_name
                else _unique(_sanitise_name(class_name))
            )
        else:
            raise ValueError(
                f"classes[{idx}]: expected string or mapping, got {type(entry).__name__}"
            )
        params = normalize_sbk_params(params, context=f"classes[{idx}]")
        out.append(Instance(name=name, class_name=class_name, params=params))

    # check name uniqueness (explicit names could collide)
    seen: set[str] = set()
    for inst in out:
        if inst.name in seen:
            raise ValueError(
                f"duplicate instance name {inst.name!r}; set unique 'name:' values"
            )
        seen.add(inst.name)
    return out
