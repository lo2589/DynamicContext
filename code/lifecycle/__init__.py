"""Lifecycle registrations."""

from ..registry import lifecycle
from .rules import (  # noqa: F401  (registers lifecycle.born, born+n, permanent, until_*)
    DYNAMIC_RULE_PREFIX,
    is_dynamic_rule,
    resolve_bound,
    str2func,
)
from .span import INF, Remain, calculate_end_turn, validate_remain  # noqa: F401

__all__ = [
    "lifecycle",
    "DYNAMIC_RULE_PREFIX",
    "INF",
    "Remain",
    "calculate_end_turn",
    "is_dynamic_rule",
    "resolve_bound",
    "str2func",
    "validate_remain",
]
