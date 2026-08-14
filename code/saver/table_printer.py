"""Console table snapshots for committed runtime history, state, and context."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Iterable

from ..registry import saver


def _display_width(value: str) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        for char in value
    )


def _clip(value: Any, limit: int) -> str:
    text = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if _display_width(text) <= limit:
        return text
    result: list[str] = []
    width = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if width + char_width > max(0, limit - 1):
            break
        result.append(char)
        width += char_width
    return "".join(result) + "…"


def _pad(value: str, width: int) -> str:
    return value + " " * (width - _display_width(value))


def _table(
    title: str,
    headers: list[str],
    rows: Iterable[Iterable[Any]],
    *,
    limits: list[int],
) -> str:
    normalized = [
        [_clip(value, limits[index]) for index, value in enumerate(row)]
        for row in rows
    ]
    widths = [
        max(
            _display_width(headers[index]),
            *(_display_width(row[index]) for row in normalized),
        )
        for index in range(len(headers))
    ]
    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"
    lines = [f"=== {title} ===", border]
    lines.append(
        "| "
        + " | ".join(_pad(header, widths[index]) for index, header in enumerate(headers))
        + " |"
    )
    lines.append(border)
    for row in normalized:
        lines.append(
            "| "
            + " | ".join(_pad(value, widths[index]) for index, value in enumerate(row))
            + " |"
        )
    lines.append(border)
    return "\n".join(lines)


def _context_turns(
    history: dict[str, dict[str, dict[str, Any]]],
    state: dict[str, dict[str, int]],
) -> list[str]:
    """Which turn each context message came from, walked in the exact same
    order select_context_without_extra_filter builds context in — so a
    printed row can be traced back to the turn that produced it."""

    turns: list[str] = []
    for stored_turn in sorted(history, key=int):
        for element in history[stored_turn]:
            if state.get(stored_turn, {}).get(element, 0) != 1:
                continue
            turns.append(stored_turn)
    return turns


@saver("print.none")
def print_no_tables(**_: Any) -> None:
    return None


@saver("print.tables")
def print_runtime_tables(
    *,
    history: dict[str, dict[str, dict[str, Any]]],
    state: dict[str, dict[str, int]],
    context: list[dict[str, str]],
    turn_id: int,
) -> None:
    history_rows = []
    for stored_turn in sorted(history, key=int):
        for element, slot in history[stored_turn].items():
            history_rows.append(
                (
                    stored_turn,
                    element,
                    slot.get("content", ""),
                    json.dumps(
                        slot.get("range", []),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )
            )

    state_rows = []
    for stored_turn in sorted(state, key=int):
        for element, value in state[stored_turn].items():
            state_rows.append((stored_turn, element, value))

    context_turns = _context_turns(history, state)
    context_rows = [
        (
            context_turns[index] if index < len(context_turns) else "-",
            message.get("role", ""),
            message.get("content", ""),
        )
        for index, message in enumerate(context)
    ]

    print()
    print(
        _table(
            f"TURN {turn_id} · HISTORY",
            ["turn", "element", "content", "range"],
            history_rows,
            limits=[8, 18, 72, 42],
        )
    )
    print(
        _table(
            f"TURN {turn_id} · STATE",
            ["turn", "element", "state"],
            state_rows,
            limits=[8, 18, 8],
        )
    )
    print(
        _table(
            f"TURN {turn_id} · CONTEXT",
            ["turn", "role", "content"],
            context_rows,
            limits=[8, 12, 96],
        )
    )


def _self_test() -> None:
    import contextlib
    import io

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        print_runtime_tables(
            history={
                "1": {
                    "user": {
                        "content": "你好",
                        "range": [[[1, None], 1.0, "none"]],
                    }
                }
            },
            state={"1": {"user": 1}},
            context=[{"role": "user", "content": "你好"}],
            turn_id=1,
        )
    rendered = output.getvalue()
    assert "TURN 1 · HISTORY" in rendered
    assert "TURN 1 · STATE" in rendered
    assert "TURN 1 · CONTEXT" in rendered
    assert "你好" in rendered

    # CONTEXT rows show the turn each message came from, not a flat row index.
    assert _context_turns(
        history={
            "1": {
                "user": {"content": "u1", "range": [[[1, None], 1.0, "none"]]},
                "think": {"content": "t1", "range": [[[1, 1], 1.0, "none"]]},
                "assistant": {"content": "a1", "range": [[[1, None], 1.0, "none"]]},
            },
            "2": {
                "user": {"content": "u2", "range": [[[2, None], 1.0, "none"]]},
            },
        },
        state={
            "1": {"user": 1, "think": 0, "assistant": 1},
            "2": {"user": 1},
        },
    ) == ["1", "1", "2"]


if __name__ == "__main__":
    _self_test()
    print("saver.table_printer: ok")
