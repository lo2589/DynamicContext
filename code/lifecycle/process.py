"""One question, asked the same way every time: where does this range end?

The functions that answer it live in ``rules.py``, each registered where it is
written. Whichever one a declaration names is handed to :func:`process` as a
handle, so there is one place a range acquires an end and one calling convention
for the functions that decide it::

    process(history, table, range_end_func=cycle)
    process(history, table, range_end_func=labelled)

Only the functions that need the current turn are ever handed over. ``born`` and
``permanent`` answer from the birth turn alone, so their answer is already
written into the range and asking again would be asking a settled question.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return self._name


#: Not on stage this turn. The covering range, if any, ends before it.
GONE = _Sentinel("GONE")
#: No end. Written into a range as an open right edge.
OPEN = None


def process(
    history: Mapping[str, Any],
    table: Any,
    *,
    range_end_func: Any,
    elements: Any,
    turn: int,
    **arguments: Any,
) -> None:
    """Ask ``range_end_func`` where each of ``elements``' cells ends, and record it.

    ``table`` closes and reopens ranges; this decides nothing about how a range
    is written. Extra keyword arguments pass straight through to the function,
    so this layer never has to know what any of them needs.
    """

    governed = set(elements or ())
    if not governed:
        return

    for raw_turn, slots in history.items():
        born_turn = int(raw_turn)
        if born_turn >= turn or not isinstance(slots, Mapping):
            continue
        for element in governed & set(slots):
            slot = slots[element]
            if not isinstance(slot, Mapping):
                continue
            answer = range_end_func(
                element=element, born_turn=born_turn, turn=turn, **arguments
            )
            standing = _covering(slot.get("range"), turn)
            if answer is GONE:
                if standing is not None:
                    table.end_cell(element, born_turn, turn=turn)
            elif standing is None:
                table.reopen_cell(element, born_turn, turn=turn)
            elif answer is not OPEN and int(answer) < turn:
                table.end_cell(element, born_turn, turn=turn)


def _covering(ranges: Any, turn: int) -> Optional[list[Any]]:
    """The segment that puts a cell on stage at ``turn``, if one does."""

    if not isinstance(ranges, list):
        return None
    for entry in ranges:
        if not isinstance(entry, list) or len(entry) != 3:
            continue
        scope = entry[0]
        if not isinstance(scope, list) or len(scope) != 2:
            continue
        start, end = scope
        if start is None or int(start) > turn:
            continue
        if end is None or int(end) >= turn:
            return entry
    return None
