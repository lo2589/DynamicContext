"""Pin: attach a permanent note to an already-committed turn.

A pin is not a new exchange — it's a note about what already happened, so
creating or cancelling one reuses the small standalone-transaction pattern
(recompute from the just-saved tables, mutate, validate, save again) rather
than going through a full turn.
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING, Any

from ..manager.engine import FixedTableLifecycle
from ..registry import manager, saver

if TYPE_CHECKING:
    from ..manager.runtime import RuntimeComponents

    History = dict[str, dict[str, dict[str, Any]]]

# pin_{origin}_{id} — origin (model/user) and id are both visible straight in
# the element name, the same way role is visible in "user"/"assistant"
# without a separate metadata table. Ids never repeat and are never
# recycled after a /cancelpin: they're derived from the highest id ever
# seen in history, not a remembered counter — same self-healing reasoning
# as the compaction gap fix (see _first_uncompressed_turn in
# code/compact/pipeline.py).
PIN_ELEMENT_PATTERN = re.compile(r"^pin_(?:model|user)_p(\d+)$")


def _next_pin_id(history: "History") -> str:
    numbers = [
        int(match.group(1))
        for content in history.values()
        for element in content
        if (match := PIN_ELEMENT_PATTERN.match(element))
    ]
    return f"p{(max(numbers) + 1) if numbers else 1:02d}"


def _pin_element_name(origin: str, pin_id: str) -> str:
    return f"pin_{origin}_{pin_id}"


def _find_pin_element(history: "History", pin_id: str) -> tuple[str, int] | None:
    """Locate the (element_name, born_turn) for a bare id like "p01" —
    /cancelpin doesn't know (or need to know) which origin created it."""

    for turn_id, content in history.items():
        for element in content:
            match = PIN_ELEMENT_PATTERN.match(element)
            if match and f"p{int(match.group(1)):02d}" == pin_id:
                return element, int(turn_id)
    return None


def _create_pin(runtime: "RuntimeComponents", content: str, *, origin: str) -> None:
    """/pin: the same small standalone-transaction pattern _maybe_compress
    uses — recompute from the just-saved tables, mutate, validate, save
    again. Attaches to the last committed turn rather than starting a new
    one; a pin is a note about what already happened, not a new exchange."""

    from ..manager.runtime import _last_turn  # cycle guard: runtime imports us at module level

    current_turn = _last_turn(runtime.tables.history)
    if current_turn < 1:
        print("[pin skipped: no committed turn to attach to yet]")
        return
    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    element = _pin_element_name(origin, _next_pin_id(history))
    lifecycle.add_slot(element, content, turn=current_turn)
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    print(f"[pin created: {element}] {content}")


def _cancel_pin(runtime: "RuntimeComponents", pin_id: str) -> None:
    from ..manager.runtime import _last_turn  # cycle guard: runtime imports us at module level

    current_turn = _last_turn(runtime.tables.history)
    found = _find_pin_element(runtime.tables.history, pin_id)
    if found is None:
        print(f"[cancelpin: no such pin {pin_id}]")
        return
    element, born_turn = found
    history = copy.deepcopy(runtime.tables.history)
    lifecycle: FixedTableLifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    # turn=current_turn + 1: the pin was genuinely active through the turn
    # that already happened (and was shown then) — cancelling doesn't
    # rewrite that, it only stops the pin from here on.
    lifecycle.end_cell(element, born_turn, turn=current_turn + 1)
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    print(f"[pin cancelled: {element}]")
