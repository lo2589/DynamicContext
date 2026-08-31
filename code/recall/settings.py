"""Recall's own config readers: what to search, when to trigger, and the
life_cycle guard that keeps recalled evidence from piling up forever.
"""

from __future__ import annotations

from typing import Any

DEFAULT_RECALL_TRIGGER = "always"
RECALL_ELEMENT = "recall"


def _recall_search_fields(cfg: Any) -> tuple[str, ...]:
    """Which elements recall is allowed to search, from recall.search_fields.

    Required whenever recall is on, and for a concrete reason: recall only
    considers retired slots, so in a config where the conversational slots
    never retire, the only thing left to find is whatever expires every turn
    — the model's own discarded reasoning. Naming the searchable evidence is
    what keeps a lookup pointed at evidence."""

    section = cfg.to_dict().get("recall")
    section = section if isinstance(section, dict) else {}
    declared = section.get("search_fields")
    if not isinstance(declared, list) or not declared:
        raise ValueError(
            "启用 recall 时必须声明 recall.search_fields："
            "回忆只搜已退役的槽位，不指明搜哪些元素就只会捞到每轮自动退役的东西"
        )
    return tuple(str(item) for item in declared if item)


def _recall_trigger_settings(cfg: Any) -> tuple[str, dict[str, Any]]:
    """Read recall.trigger. Accepts a bare name ("always") or a mapping
    carrying the trigger's own parameters ({type: pattern, pattern: ...})."""

    section = cfg.to_dict().get("recall")
    section = section if isinstance(section, dict) else {}
    trigger = section.get("trigger")
    if isinstance(trigger, str):
        return trigger, {}
    if not isinstance(trigger, dict):
        return DEFAULT_RECALL_TRIGGER, {}
    name = str(trigger.get("type") or DEFAULT_RECALL_TRIGGER)
    return name, {key: value for key, value in trigger.items() if key != "type"}


def _require_recall_life_cycle(cfg: Any) -> None:
    """An undeclared element defaults to permanent, which is right for most
    slots and quietly wrong for recall: every turn's pulled-back evidence
    would stay visible forever and pile up. Rather than hardcode a different
    default for one element name, refuse to guess and make the config say
    how long recalled evidence lives."""

    from ..manager.runtime import _choice  # cycle guard: runtime imports us at module level

    if _choice(cfg, "recall", "type", "none") == "none":
        return
    life_cycle = cfg.to_dict().get("life_cycle")
    life_cycle = life_cycle if isinstance(life_cycle, dict) else {}
    _recall_search_fields(cfg)
    if RECALL_ELEMENT not in life_cycle:
        raise ValueError(
            f"启用 recall 时必须在 life_cycle 里声明 {RECALL_ELEMENT} 的生命周期，"
            f"例如 {RECALL_ELEMENT}: [born, born]（只在提问那一轮可见）"
        )
