"""The functions a life_cycle declaration may name, and the reader that finds them.

Every one of them answers the same question — where does this range end — and
they differ only in what they need in order to answer:

  the birth turn alone   ``born``, ``born_add``, ``permanent``. Settled the
                         moment the cell is written, folded into one end, never
                         asked again.
  the current turn too   ``cycle``, ``labelled``. Cannot answer before that turn
                         arrives, so they are handed to ``process`` each turn.
  a future event         ``until_*``. Birth leaves the range open and whatever
                         detects the event closes it through ``end_cell``.

The currency is the same in every case: an end turn, ``OPEN`` for no end in
sight, ``GONE`` for not on stage this turn.

There is no ``none`` here. Writing nothing is not naming a function — it is the
empty value, and reading it is the reader's business, not the registry's.
"""

from __future__ import annotations

from inspect import signature
from typing import Any, Optional

from ..registry import lifecycle
from .process import GONE, OPEN


DYNAMIC_RULE_PREFIX = "until_"


def each_turn(func: Any) -> Any:
    """Mark a function as one that cannot answer until the turn arrives.

    Whether a function must be asked again every turn is a property of the
    function, so it is written on the function. Reading it off the signature
    instead — asking whether a ``turn`` parameter happens to be there — makes the
    property something inferred from an accident of how the code was typed, and a
    function that takes ``turn`` but forgot to say so would quietly become one
    that is only asked at birth. That is exactly the failure that once let a rule
    meant for one element revive every other element's expired interval.

    Unmarked means settled at birth, which is the answer that touches nothing.
    """

    func.each_turn = True
    return func


@lifecycle("born")
def born(*, born_turn: int, **_: Any) -> int:
    """Ends on the turn it was born."""

    return born_turn


@lifecycle("born_add")
def born_add(*, born_turn: int, n: int, **_: Any) -> int:
    """Ends ``n`` turns after the turn it was born.

    The ``n`` in ``born+3`` is an argument, not part of a name. The reader splits
    the plus off and hands the number over, so nothing downstream ever sees a
    string with a number baked into it.
    """

    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        raise ValueError("born+n 的 n 必须是非负整数")
    return born_turn + n


@lifecycle("permanent")
def permanent(**_: Any) -> Any:
    """Never ends. What an empty declaration means."""

    return OPEN


@lifecycle("until_cancelled")
def until_cancelled(**_: Any) -> Any:
    """Stays open until an explicit cancel closes the cell."""

    return OPEN


@lifecycle("until_goal_end")
def until_goal_end(**_: Any) -> Any:
    """Stays open until the governing goal's own range closes."""

    return OPEN


@lifecycle("cycle")
@each_turn
def cycle(*, born_turn: int, source: Any, turn: int, **_: Any) -> Any:
    """Off stage for as long as the turn being answered points back at this one.

    ``source`` is the input handle, handed over by the caller. What the link
    means is the dataset's business; this only learns that answering this turn
    concerns that one, and steps that one aside while it is answered. It cannot
    say so before that turn arrives, which is why it takes ``turn``.
    """

    return GONE if source.linked_trap(turn) == born_turn else OPEN


@lifecycle("labelled")
@each_turn
def labelled(*, born_turn: int, source: Any, turn: int, **_: Any) -> Any:
    """``born`` for the turns the input labelled, ``permanent`` for the rest.

    Not a third kind of end. A labelled turn ends on itself and an unlabelled one
    never ends; both answers already have functions, and this only asks the input
    which of the two a cell gets. Judged by turn rather than by element name, so a
    turn's question and the answer it drew leave together.

    What a label means is the dataset's business. A different set of labels is a
    different function, not an argument parsed out of a string.
    """

    if source.turn_type(born_turn) not in {"trap", "recall"}:
        return permanent()
    return GONE if born_turn < turn else born(born_turn=born_turn)


def asks_each_turn(name: str) -> bool:
    """Whether the function this name stands for said it must be asked each turn.

    Read off the function, where :func:`each_turn` wrote it. A name nobody
    registered, or a function that said nothing, answered at birth.
    """

    try:
        return bool(getattr(str2func(name), "each_turn", False))
    except KeyError:
        return False


def is_dynamic_rule(name: str) -> bool:
    return name.startswith(DYNAMIC_RULE_PREFIX)


def str2func(name: str) -> Any:
    """The function a written name stands for.

    The registry is what turns a name into a function, and this is the name that
    says so. Anything a declaration may name has to be registered, which is also
    what makes a typo a loud error naming everything that was available.
    """

    return lifecycle.get(name.strip())


def end_functions(life_cycle: Any) -> dict[Any, list[str]]:
    """The end functions a declaration names, each with the elements naming it.

    Only the ones that cannot answer until the turn arrives are listed, because
    those are the only ones ``process`` has anything to ask.
    """

    grouped: dict[Any, list[str]] = {}
    for element, bound in (life_cycle or {}).items():
        if not isinstance(bound, (list, tuple)) or len(bound) != 2:
            continue
        name = bound[1]
        if not isinstance(name, str) or not asks_each_turn(name.strip()):
            continue
        grouped.setdefault(str2func(name), []).append(element)
    return grouped


def resolve_bound(bound: Any, born_turn: int) -> Optional[int]:
    """Read one written bound and get back the turn it names.

    What YAML hands over is a string, so somebody has to read it, and this is the
    only place that does. Exactly two forms are read rather than looked up:

      empty  — nothing written. The empty value means no end, so it is
               ``permanent``, and this branch is the whole of its logic.
      a plus — punctuation, split off so the number after it can be handed over
               as an argument. ``born+3`` becomes ``born_add(n=3)``.

    Everything else goes straight to :func:`str2func`. Written a number rather
    than a name there is nothing to convert, and it is the turn it says.
    """

    if bound is None:
        return permanent(born_turn=born_turn)
    if isinstance(bound, bool):
        raise ValueError("life_cycle 边界不能是 bool")
    if not isinstance(bound, str):
        return int(bound)
    name = bound.strip()
    if not name:
        raise ValueError("life_cycle 边界不能是空字符串")
    if "+" in name:
        base, _, argument = name.partition("+")
        return str2func(f"{base.strip()}_add")(born_turn=born_turn, n=int(argument))
    if asks_each_turn(name):
        # Not knowable at birth by definition; the interval stays open and each
        # turn decides for itself.
        return None
    return str2func(name)(born_turn=born_turn)


def _self_test() -> None:
    # Every name a declaration may write resolves through str2func, and every
    # function is registered where it is written.
    for name in ("born", "born_add", "permanent", "cycle", "labelled"):
        assert str2func(name) is globals()[name], name
    # none is not a function and is not registered; writing nothing is the
    # empty value, and the reader is where that means permanent.
    assert "none" not in lifecycle.names()
    assert resolve_bound(None, 7) == permanent(born_turn=7) is None

    # A number is the turn it says; a name is the function it names.
    assert resolve_bound(1, 7) == 1
    assert resolve_bound("born", 7) == 7
    assert resolve_bound("permanent", 7) is None
    # The plus is punctuation the reader eats; born_add is handed a number.
    assert resolve_bound("born+3", 7) == born_add(born_turn=7, n=3) == 10
    assert resolve_bound("born+0", 7) == born(born_turn=7) == 7
    # Dynamic rules resolve to no end at birth; the event that ends them writes
    # the real end back later through end_cell.
    assert is_dynamic_rule("until_cancelled") and not is_dynamic_rule("born+3")
    assert resolve_bound("until_cancelled", 7) is None
    assert resolve_bound("until_goal_end", 7) is None

    class _Source:
        def __init__(self, linked=None, types=None):
            self._linked, self._types = linked or {}, types or {}
        def linked_trap(self, turn): return self._linked.get(turn)
        def turn_type(self, turn): return self._types.get(turn)

    # cycle steps a turn aside for exactly as long as it is being answered.
    source = _Source(linked={9: 7})
    assert cycle(born_turn=7, source=source, turn=9) is GONE
    assert cycle(born_turn=7, source=source, turn=10) is OPEN
    assert cycle(born_turn=8, source=source, turn=9) is OPEN

    # labelled hands back one of the other two answers rather than inventing a
    # third: born for a labelled turn, permanent for one the input said nothing about.
    typed = _Source(types={20: "info", 21: "trap", 22: "recall"})
    assert labelled(born_turn=21, source=typed, turn=21) == born(born_turn=21)
    assert labelled(born_turn=20, source=typed, turn=23) == permanent()
    # Past its own turn, a labelled turn is off stage — question and answer alike.
    assert labelled(born_turn=21, source=typed, turn=23) is GONE
    assert labelled(born_turn=22, source=typed, turn=23) is GONE

    # The marker is the only thing consulted at runtime, so a function that needs
    # the turn but forgot to say so must not be able to slip through quietly.
    for name in lifecycle.names():
        takes_turn = "turn" in signature(str2func(name)).parameters
        assert takes_turn == asks_each_turn(name), f"{name} 签名与 @each_turn 声明不一致"
    assert asks_each_turn("cycle") and asks_each_turn("labelled")
    assert not any(asks_each_turn(n) for n in ("born", "born_add", "permanent"))
    assert not asks_each_turn("no_such_rule")

    # Only the functions that take turn are handed to process; the ones that
    # answered at birth are not asked a settled question again.
    assert end_functions({"user": ["born", "cycle"], "think": ["born", "born"]}) == {
        cycle: ["user"]
    }
    assert end_functions({"user": ["born", "permanent"]}) == {}

    for bad in ("", "born+", "born+-1", "born+x", "nope+3", "no_such_rule", "3", True):
        try:
            resolve_bound(bad, 7)
        except (KeyError, ValueError):
            pass
        else:
            raise AssertionError(f"非法生命周期边界没有报错: {bad!r}")


if __name__ == "__main__":
    _self_test()
    print("lifecycle.rules: ok")
