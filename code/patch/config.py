"""Reading ContentPatch declarations out of YAML, and materializing the ones
that belong to already-committed turns into the ledger at startup.

state_patch.py owns the patch data model and its pure application logic;
this is the layer above it that talks to cfg/tables/RuntimeComponents.
"""

from __future__ import annotations

import copy
from math import isinf
from typing import TYPE_CHECKING, Any

from ..lifecycle.span import INF
from ..manager.context import select_context_without_extra_filter
from ..manager.engine import FixedTableLifecycle
from ..registry import manager, patch, saver
from .state_patch import ContentPatch, ContentPatchMode

if TYPE_CHECKING:
    from ..dataset.tables import DatasetTables
    from ..manager.runtime import RuntimeComponents


def _configured_patches(cfg: Any) -> list[ContentPatch]:
    values = ((cfg.to_dict().get("context") or {}).get("patches") or {})
    if not isinstance(values, dict):
        raise ValueError("context.patches必须是mapping")
    patches: list[ContentPatch] = []
    for patch_id, value in values.items():
        if not isinstance(value, dict):
            raise ValueError(f"context.patches.{patch_id}必须是mapping")
        turns = value["turns"]
        patches.append(
            patch["create"](
                creator=value.get("creator", "user"),
                element=value["element"],
                content=value["content"],
                mode=value["mode"],
                turns=INF if turns == "none" else turns,
                created_turn=value["created_turn"],
            )
        )
    return sorted(patches, key=lambda item: int(item.created_turn or 0))


def _patch_end_settings(cfg: Any) -> tuple[bool, str]:
    """Read the optional ending-message switch without making it mandatory."""

    context = (cfg.to_dict().get("context") or {})
    if not isinstance(context, dict):
        return False, ""
    raw = context.get("patch_end", False)
    if isinstance(raw, bool):
        if raw:
            raise ValueError(
                "context.patch_end 为 true 时必须写成 mapping 并声明 template"
            )
        return False, ""
    if not isinstance(raw, dict):
        raise ValueError("context.patch_end必须是bool或mapping")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("context.patch_end.enabled必须是bool")
    template = raw.get("template")
    if not enabled:
        return False, str(template or "")
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "启用 context.patch_end 时必须声明 template：结束语是内容，属于配置"
        )
    try:
        template.format(element="goal", content="example")
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "context.patch_end.template只支持{element}和{content}"
        ) from exc
    return enabled, template.strip()


def _range_end(slot: dict[str, Any]) -> int | None:
    ends: list[int] = []
    for entry in slot.get("range") or []:
        try:
            end = entry[0][1]
        except (IndexError, TypeError):
            continue
        if end is None:
            return None
        ends.append(int(end))
    return max(ends) if ends else None


def _range_active_at(slot: dict[str, Any], turn: int) -> bool:
    for entry in slot.get("range") or []:
        try:
            start, end = entry[0]
        except (IndexError, TypeError, ValueError):
            continue
        if int(start) <= turn and (end is None or int(end) >= turn):
            return True
    return False


def _source_owns_occurrence(source: ContentPatch, turn: int) -> bool:
    if source.created_turn is None:
        return False
    if source.mode in (ContentPatchMode.REMAIN, ContentPatchMode.REFRESH):
        return turn == source.created_turn
    return source.created_turn <= turn < source.created_turn + source.turns


def _restore_committed_patches(cfg: Any, tables: "DatasetTables") -> None:
    """Materialize YAML patches that belong to already committed turns."""

    from ..manager.runtime import _element_roles, _last_turn  # cycle guard

    latest = _last_turn(tables.history)
    history = copy.deepcopy(tables.history)
    scheduled: list[tuple[int, int, ContentPatch]] = []
    for source_index, source_patch in enumerate(_configured_patches(cfg)):
        born = int(source_patch.created_turn or 0)
        for turn in range(born, latest + 1):
            occurrence = patch["content.for_turn"](source_patch, turn)
            if occurrence is not None:
                scheduled.append((turn, source_index, occurrence))

    for _, _, content_patch in sorted(scheduled):
        patch_turn = int(content_patch.created_turn or 0)
        turns = content_patch.turns
        range_end = (
            None
            if isinstance(turns, float) and isinf(turns)
            else patch_turn + turns - 1
        )

        expected = {
            "content": content_patch.content,
            "range": [[[patch_turn, range_end], 1.0, "none"]],
        }
        existing = history.setdefault(str(patch_turn), {}).get(
            content_patch.element
        )
        if existing != expected:
            history[str(patch_turn)].pop(content_patch.element, None)

        lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
            history,
            tables.life_cycle,
            current_turn=max(0, patch_turn - 1),
        )
        if content_patch.mode in (ContentPatchMode.ROLL, ContentPatchMode.REFRESH):
            lifecycle.end_active(content_patch.element, turn=patch_turn)
        if existing != expected:
            lifecycle.add_slot(
                content_patch.element,
                content_patch.content,
                turn=patch_turn,
                remain=content_patch.turns,
            )

    lifecycle = manager["lifecycle.fixed"](
        history,
        tables.life_cycle,
        current_turn=latest,
    )
    expected_state = lifecycle.state_snapshot()
    expected_context = select_context_without_extra_filter(
        history, expected_state, roles=_element_roles(cfg)
    )
    if history != tables.history:
        saver["history.save"](tables, history)
    if expected_state != tables.state:
        saver["state.save"](tables, expected_state)
    if expected_context != tables.context:
        saver["context.save"](tables, expected_context)


def _commit_patch_only_turn(runtime: "RuntimeComponents") -> int:
    """Commit a turn that carries nothing but this moment's due patches —
    no user, no assistant.

    A bare "/element content mode turns" submission used to be invisible:
    queue_patch stored it and the loop `continue`d, so it only became
    visible once some later real exchange happened to land on the same
    turn_id and carried the patched element alongside its own user/
    assistant slots. That made a system change look like it was piggybacking
    on whatever conversation came next, instead of being its own event in
    the ledger. This gives it a turn of its own, the same way a real
    exchange gets one — mirrors TurnTransaction.begin/prepare_input/commit's
    shape, minus the user input and provider call neither apply here.
    """

    from ..manager.runtime import _last_turn  # cycle guard: runtime imports us at module level

    turn_id = _last_turn(runtime.tables.history) + 1
    patches = runtime.patches_for_turn(turn_id)
    if not patches:
        return turn_id

    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=turn_id - 1
    )
    for content_patch in patches:
        if content_patch.mode in (ContentPatchMode.ROLL, ContentPatchMode.REFRESH):
            lifecycle.end_active(content_patch.element, turn=turn_id)
        lifecycle.end_active(f"{content_patch.element}_end", turn=turn_id)

    lifecycle.expire_turn(turn_id)

    for content_patch in patches:
        lifecycle.add_slot(
            content_patch.element,
            content_patch.content,
            turn=turn_id,
            remain=content_patch.turns,
        )

    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=turn_id, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    runtime.consume_patches(turn_id)
    return turn_id
