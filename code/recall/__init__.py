"""Recall registrations."""

from ..registry import recall


@recall("none")
def no_recall(*_args, **_kwargs):
    return None


from .grep_recall import grep_history  # noqa: E402  (registers recall.grep)
from .trigger import trigger_always  # noqa: E402  (registers recall.trigger.*)

__all__ = ["recall", "grep_history", "trigger_always"]
