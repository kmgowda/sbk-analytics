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
SBK_INTERFACE_POLICY = RUNTIME_POLICY.sbk_interface
SBK_CONTRACT_POLICY = RUNTIME_POLICY.sbk_contract


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
    deprecated_cleanup = SBK_CONTRACT_POLICY.deprecated_cleanup_option
    cleanup = SBK_CONTRACT_POLICY.cleanup_option
    if deprecated_cleanup in keys:
        old_key = keys[deprecated_cleanup]
        if cleanup in keys:
            raise ValueError(
                f"{context}: use only '{cleanup}'; "
                f"'{deprecated_cleanup}' was renamed"
            )
        normalized[cleanup] = normalized.pop(old_key)
        log.warning(
            "%s: migrated deprecated SBK option '%s' to '%s'",
            context, deprecated_cleanup, cleanup,
        )
        keys = _key_map(normalized)

    for option, guidance in SBK_CONTRACT_POLICY.removed_gem_options:
        if option in keys:
            raise ValueError(
                f"{context}: SBK removed option '{keys[option]}'; {guidance}"
            )

    nodes_option = SBK_INTERFACE_POLICY.nodes_option
    nodes = (
        normalized.get(keys.get(nodes_option, ""))
        if nodes_option in keys else None
    )
    if isinstance(nodes, (list, tuple)):
        is_gem = any(str(node).strip() for node in nodes)
    else:
        is_gem = nodes is not None and bool(str(nodes).strip())
    if not is_gem:
        invalid = sorted(
            option for option in SBK_CONTRACT_POLICY.gem_only_options
            if option in keys
        )
        if invalid:
            raise ValueError(
                f"{context}: SBK-GEM option(s) {', '.join(invalid)} require a non-empty 'nodes' value"
            )

    for option in SBK_CONTRACT_POLICY.boolean_options:
        if option in keys:
            _parse_bool(normalized[keys[option]], option)
    for option in SBK_CONTRACT_POLICY.positive_integer_options:
        if option in keys:
            _positive_integer(normalized[keys[option]], option)
    for option in SBK_CONTRACT_POLICY.nonnegative_integer_options:
        if option in keys:
            _nonnegative_integer(normalized[keys[option]], option)
    for option in SBK_CONTRACT_POLICY.positive_decimal_options:
        if option in keys:
            _positive_decimal(normalized[keys[option]], option)

    for left, right in SBK_CONTRACT_POLICY.mutually_exclusive_options:
        if left in keys and right in keys:
            raise ValueError(
                f"{context}: SBK options '{left}' and '{right}' are mutually exclusive"
            )
    seconds_option = SBK_INTERFACE_POLICY.seconds_option
    if (
        SBK_CONTRACT_POLICY.total_records_option in keys
        and SBK_CONTRACT_POLICY.total_throughput_option in keys
        and seconds_option in keys
    ):
        try:
            timed = int(normalized[keys[seconds_option]]) > 0
        except (TypeError, ValueError):
            timed = False
        if timed:
            raise ValueError(
                f"{context}: '{SBK_CONTRACT_POLICY.total_records_option}' and "
                f"'{SBK_CONTRACT_POLICY.total_throughput_option}' cannot both "
                f"be used with '{seconds_option}'"
            )
    return normalized
