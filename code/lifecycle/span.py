"""How long a cell lives: the lifespan vocabulary itself.

Separate from rules.py, which resolves a declared *bound* into a concrete
turn. This module holds the primitives that any lifespan is expressed in —
the value meaning "never expires", the type a lifespan may take, and what
counts as a well-formed one. They live here rather than inside the
lifecycle state machine because compaction and the state machine both need
to speak this vocabulary, and neither should have to import the other to
do so.
"""

from __future__ import annotations

from math import inf, isinf
from typing import Union


INF = inf
Remain = Union[int, float]


def validate_remain(remain: Remain) -> None:
    """A lifespan is a positive whole number of turns, or INF."""

    if isinstance(remain, bool):
        raise ValueError("remain 不能是 bool")
    if isinstance(remain, float) and isinf(remain):
        return
    if not isinstance(remain, int) or remain < 1:
        raise ValueError("remain 必须是正整数或 INF")


def calculate_end_turn(born_turn: int, remain: Remain) -> Remain:
    """The exclusive end turn a cell born at born_turn reaches."""

    if isinstance(remain, float) and isinf(remain):
        return INF
    return born_turn + int(remain)


def _self_test() -> None:
    validate_remain(1)
    validate_remain(99)
    validate_remain(INF)
    for bad in (0, -1, 1.5, True, False, "1", None):
        try:
            validate_remain(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法 remain 没有报错: {bad!r}")

    assert calculate_end_turn(5, 1) == 6
    assert calculate_end_turn(5, 3) == 8
    assert calculate_end_turn(5, INF) == INF


if __name__ == "__main__":
    _self_test()
    print("lifecycle.span: ok")
