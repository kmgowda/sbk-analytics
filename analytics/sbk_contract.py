#!/usr/bin/python3
# Copyright (c) KMG. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Current SBK option contract and migration rules."""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from .policy import RUNTIME_POLICY

log = logging.getLogger(__name__)

_REMOVED_GEM_OPTIONS = {
    "copyonlydrivers": "use 'fullcopy: false' for compact driver-scoped provisioning",
    "compactruntimecopy": "use 'fullcopy' with the former value inverted",
    "compactcopy": "use 'fullcopy' with the former value inverted",
    "copy": "runtime content is now provisioned automatically",
    "deleteafter": "use 'packagescleanup' to control stale managed packages",
    "delete": "invalid managed runtimes are repaired automatically",
    "sbkcommand": "SBK-GEM now selects the standard launcher itself",
    "sbkdir": "SBK-GEM now receives its application home from its launcher",
    "javacopy": "SBK-GEM now provisions or reuses Java automatically",
    "javaversion": "configure sbk.jdk.version in sbk-config.env instead",
}
_GEM_ONLY_OPTIONS = {
    "gemuser", "gempass", "hostkeycheck", "knownhosts", "gemport", "javadir",
    "packagescleanup", "fullcopy", "localhost", "sbmport", "sbmsleepms",
    "totalrecords", "totalthroughput",
}
_BOOLEAN_OPTIONS = {"hostkeycheck", "packagescleanup", "fullcopy"}
_POSITIVE_INTEGER_OPTIONS = {
    "idletimeoutseconds", "gemport", "sbmport", "totalrecords",
}
_NONNEGATIVE_INTEGER_OPTIONS = {"sbmsleepms"}


def _key_map(params: dict[str, Any]) -> dict[str, str]:
    return {str(key).strip().lower().lstrip("-"): key for key in params}


def _parse_bool(value: Any, option: str) -> None:
    if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
        return
    if isinstance(value, str) and value.strip().lower() in {
        *RUNTIME_POLICY.configuration.true_tokens,
        *RUNTIME_POLICY.configuration.false_tokens,
    }:
        return
    raise ValueError(f"SBK option '{option}' must be a boolean, got {value!r}")


def _positive_integer(value: Any, option: str) -> None:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"SBK option '{option}' must be a positive integer, got {value!r}"
        ) from exc
    if parsed <= 0:
        raise ValueError(
            f"SBK option '{option}' must be a positive integer, got {value!r}"
        )


def _positive_decimal(value: Any, option: str) -> None:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(
            f"SBK option '{option}' must be a positive number, got {value!r}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(
            f"SBK option '{option}' must be a positive number, got {value!r}"
        )


def _nonnegative_integer(value: Any, option: str) -> None:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"SBK option '{option}' must be a non-negative integer, got {value!r}"
        ) from exc
    if parsed < 0:
        raise ValueError(
            f"SBK option '{option}' must be a non-negative integer, got {value!r}"
        )


def normalize_sbk_params(params: dict[str, Any], *, context: str) -> dict[str, Any]:
    """Return parameters validated against the supported SBK contract."""
    normalized = dict(params)
    keys = _key_map(normalized)
    if "runtimecleanup" in keys:
        old_key = keys["runtimecleanup"]
        if "packagescleanup" in keys:
            raise ValueError(
                f"{context}: use only 'packagescleanup'; 'runtimecleanup' was renamed"
            )
        normalized["packagescleanup"] = normalized.pop(old_key)
        log.warning(
            "%s: migrated deprecated SBK option 'runtimecleanup' to 'packagescleanup'",
            context,
        )
        keys = _key_map(normalized)

    for option, guidance in _REMOVED_GEM_OPTIONS.items():
        if option in keys:
            raise ValueError(
                f"{context}: SBK removed option '{keys[option]}'; {guidance}"
            )

    nodes = normalized.get(keys.get("nodes", "")) if "nodes" in keys else None
    if isinstance(nodes, (list, tuple)):
        is_gem = any(str(node).strip() for node in nodes)
    else:
        is_gem = nodes is not None and bool(str(nodes).strip())
    if not is_gem:
        invalid = sorted(option for option in _GEM_ONLY_OPTIONS if option in keys)
        if invalid:
            raise ValueError(
                f"{context}: SBK-GEM option(s) {', '.join(invalid)} require a non-empty 'nodes' value"
            )

    for option in _BOOLEAN_OPTIONS:
        if option in keys:
            _parse_bool(normalized[keys[option]], option)
    for option in _POSITIVE_INTEGER_OPTIONS:
        if option in keys:
            _positive_integer(normalized[keys[option]], option)
    for option in _NONNEGATIVE_INTEGER_OPTIONS:
        if option in keys:
            _nonnegative_integer(normalized[keys[option]], option)
    if "totalthroughput" in keys:
        _positive_decimal(normalized[keys["totalthroughput"]], "totalthroughput")

    conflicts = (
        ("totalrecords", "records"),
        ("totalrecords", "throughput"),
        ("totalthroughput", "throughput"),
    )
    for left, right in conflicts:
        if left in keys and right in keys:
            raise ValueError(
                f"{context}: SBK options '{left}' and '{right}' are mutually exclusive"
            )
    if "totalrecords" in keys and "totalthroughput" in keys and "seconds" in keys:
        try:
            timed = int(normalized[keys["seconds"]]) > 0
        except (TypeError, ValueError):
            timed = False
        if timed:
            raise ValueError(
                f"{context}: 'totalrecords' and 'totalthroughput' cannot both be used with 'seconds'"
            )
    return normalized
