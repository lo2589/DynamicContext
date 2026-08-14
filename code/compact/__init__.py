"""Compact registrations."""

from ..registry import compact
from .collapse import collapse_goal_scope  # noqa: F401  (registers compact.goal_collapse)
from .summery import (  # noqa: F401  (registers compact.summary and compact.retain.*)
    DEFAULT_RETENTION,
    compute_summary,
    retain_equidistant,
    retain_keep_all,
    retain_last_k,
    retain_latest_only,
)

__all__ = [
    "compact",
    "DEFAULT_RETENTION",
    "collapse_goal_scope",
    "compute_summary",
    "retain_equidistant",
    "retain_keep_all",
    "retain_last_k",
    "retain_latest_only",
]
