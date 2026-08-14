"""By-line and full JSONL storage for the fixed history dictionary."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..registry import dataset


Slot = dict[str, Any]
TurnContent = dict[str, Slot]
History = dict[str, TurnContent]


@dataset("history.save_byline")
def save_byline(
    path: str | Path,
    turn_id: int,
    content: Mapping[str, Mapping[str, Any]],
) -> None:
    """Append exactly one turn: ``{"id": turn_id, "content": {...}}``."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {"id": turn_id, "content": content},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        )


@dataset("history.save")
def save_history(
    path: str | Path,
    history: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> None:
    """Store the complete history while keeping one physical line per turn."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for turn_id, content in history.items():
            handle.write(
                json.dumps(
                    {"id": int(turn_id), "content": content},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )


@dataset("history.load")
def load_history(path: str | Path) -> History:
    """Load JSONL turns into ``history[turn_id] = content``."""

    source = Path(path)
    if not source.exists():
        return {}
    history: History = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        turn = json.loads(line)
        history[str(turn["id"])] = turn["content"]
    return history


@dataset("history.initialize")
def initialize_history(
    history_path: str | Path,
    system: Mapping[str, Any],
) -> History:
    """Resume the configured history table, or create turn 0 in that table."""

    configured_path = Path(history_path)
    history = load_history(configured_path)
    if history:
        return history
    save_byline(configured_path, 0, {"system": system})
    return load_history(configured_path)


def _self_test() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "configured-history.jsonl"
        history = initialize_history(
            path,
            {"content": "system", "range": [[[0, None], 1.0, "none"]]},
        )
        assert not (Path(temporary) / "history.jsonl").exists()
        assert history == {
            "0": {
                "system": {
                    "content": "system",
                    "range": [[[0, None], 1.0, "none"]],
                }
            }
        }
        save_byline(
            path,
            1,
            {
                "user": {
                    "content": "hello",
                    "range": [[[1, None], 1.0, "none"]],
                },
                "think": {
                    "content": "thinking",
                    "range": [[[1, 1], 0.0, "strip"]],
                },
                "assistant": {
                    "content": "hi",
                    "range": [[[1, None], 1.0, "none"]],
                },
            },
        )
        loaded = load_history(path)
        assert loaded["1"]["assistant"]["content"] == "hi"
        assert len(path.read_text(encoding="utf-8").splitlines()) == 2
        loaded["1"]["assistant"]["range"][0][0][1] = 5
        save_history(path, loaded)
        assert load_history(path) == loaded


if __name__ == "__main__":
    _self_test()
    print("dataset.history: ok")
