"""In-place compaction: fold what a governing element covered into itself.

Unlike summary compaction, nothing is computed here — the governing
element's own content already stands in for the stretch it governed, so the
covered elements simply stop being individually visible. That makes this the
cheapest compressor there is: no model call at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..registry import compact

if TYPE_CHECKING:  # avoids a cycle: manager.engine already imports compact
    from ..manager.engine import FixedTableLifecycle


@compact("goal_collapse")
def collapse_goal_scope(
    lifecycle: "FixedTableLifecycle",
    *,
    goal_element: str,
    goal_born_turn: int,
    goal_end_turn: int,
    collapse_elements: tuple[str, ...],
) -> list[tuple[str, int]]:
    """Fold everything a goal governed into the goal itself.

    No new value is computed — the goal's own content already stands in as
    the summary for [goal_born_turn, goal_end_turn). The governed elements
    simply stop being individually visible. Returns the (element, born_turn)
    pairs actually closed, for logging/testing.

    collapse_elements is required rather than defaulted: which elements a
    compressor may fold is configuration (compact.fields), never a list
    baked into the compressor.
    """
    if goal_element in collapse_elements:
        raise ValueError(
            "goal_element 不能出现在 collapse_elements 里，否则摘要本身也被折叠了"
        )
    turn = lifecycle.current_turn
    closed: list[tuple[str, int]] = []
    for turn_id_str in sorted(lifecycle.history, key=int):
        born = int(turn_id_str)
        if not (goal_born_turn <= born < goal_end_turn):
            continue
        content = lifecycle.history[turn_id_str]
        for element in collapse_elements:
            if element not in content:
                continue
            lifecycle.end_cell(element, born, turn=turn)
            closed.append((element, born))
    return closed


def _self_test() -> None:
    from ..manager.engine import build_fixed_table_lifecycle

    history: dict[str, Any] = {
        "0": {"system": {"content": "s", "range": [[[1, None], 1.0, "none"]]}},
        "1": {
            "user": {"content": "u1", "range": [[[1, None], 1.0, "none"]]},
            "assistant": {"content": "a1", "range": [[[1, None], 1.0, "none"]]},
        },
        "2": {
            "goal": {"content": "g", "range": [[[2, 3], 1.0, "none"]]},
            "user": {"content": "u2", "range": [[[2, None], 1.0, "none"]]},
            "assistant": {"content": "a2", "range": [[[2, None], 1.0, "none"]]},
        },
        "3": {
            "user": {"content": "u3", "range": [[[3, None], 1.0, "none"]]},
            "assistant": {"content": "a3", "range": [[[3, None], 1.0, "none"]]},
        },
        "4": {
            "user": {"content": "u4", "range": [[[4, None], 1.0, "none"]]},
            "assistant": {"content": "a4", "range": [[[4, None], 1.0, "none"]]},
        },
    }
    life_cycle = {
        "system": [1, None],
        "user": ["born", None],
        "assistant": ["born", None],
        "goal": [2, 3],
    }
    lifecycle = build_fixed_table_lifecycle(history, life_cycle, current_turn=4)
    assert lifecycle.state_snapshot()["2"]["goal"] == 0  # already expired

    closed = collapse_goal_scope(
        lifecycle,
        goal_element="goal",
        goal_born_turn=2,
        goal_end_turn=4,
        collapse_elements=("user", "assistant", "think"),
    )
    assert set(closed) == {("user", 2), ("assistant", 2), ("user", 3), ("assistant", 3)}
    snapshot = lifecycle.state_snapshot()
    assert snapshot["2"]["user"] == 0
    assert snapshot["2"]["assistant"] == 0
    assert snapshot["3"]["user"] == 0
    assert snapshot["3"]["assistant"] == 0
    assert snapshot["1"]["user"] == 1  # outside the span, untouched
    assert snapshot["4"]["user"] == 1  # outside the span, untouched

    try:
        collapse_goal_scope(
            lifecycle,
            goal_element="user",
            goal_born_turn=2,
            goal_end_turn=4,
            collapse_elements=("user",),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("goal_element混进collapse_elements应该拒绝")


if __name__ == "__main__":
    _self_test()
    print("compact.collapse: ok")
