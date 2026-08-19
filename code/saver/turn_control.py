"""Hide, restore, or delete a turn — the two are not the same thing.

A model answers off the point, misreads the question, or heads the wrong way.
The turn is written; the question is what it should go on contributing.

**Hiding** closes the interval on those records. Not one character of content
changes: the ledger keeps saying this was written, and the interval now also
says when it stopped being in view — which is exactly what an interval is for
("账本里那条内容一个字没动，区间列表只是记录了它曾经、以及此刻是否出场").
The model stops seeing it next turn; the transcript still shows it, greyed,
and it can be brought back.

**Deleting** removes the records. The conversation then reads as though the
turn never happened, and nothing can restore it. That is a real loss of the
record, so it is a separate verb rather than a stronger kind of hiding —
choosing it should feel like choosing it.

Both go through the same lifecycle engine and savers every other write uses,
so state and context are re-projected the ordinary way.
"""

from __future__ import annotations

import copy
from typing import Any

from ..registry import manager, saver


def _project_and_save(runtime: Any, history: dict, current_turn: int) -> dict:
    """Re-derive state and context from the edited ledger, then persist."""

    lifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    lifecycle.validate()
    state = lifecycle.state_snapshot()
    context = runtime.select_context(
        history, state, through_turn=current_turn, roles=runtime.element_roles
    )
    saver["history.save"](runtime.tables, history)
    saver["state.save"](runtime.tables, state)
    saver["context.save"](runtime.tables, context)
    return {
        "visible": sum(1 for row in state.values() for value in row.values() if value == 1),
        "context_messages": len(context),
    }


def _targets(content: dict, element: str | None) -> list[str]:
    if element:
        if element not in content:
            raise ValueError(f"这一轮没有 {element} 这个槽位")
        return [element]
    return list(content)


def _require_turn(runtime: Any, turn: int) -> dict:
    content = runtime.tables.history.get(str(turn))
    if not content:
        raise ValueError(f"没有第 {turn} 轮")
    return content


def hide_turn(runtime: Any, turn: int, element: str | None = None) -> dict:
    """Close the interval from the next turn on.

    ``turn + 1``, not ``turn``: the records genuinely were in view for the
    turn they belong to, and the model did answer with them present. Ending
    them at their own turn would rewrite that, which is the one thing the
    interval is supposed to be honest about.
    """

    current_turn = max((int(key) for key in runtime.tables.history), default=0)
    content = _require_turn(runtime, turn)
    history = copy.deepcopy(runtime.tables.history)
    lifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    hidden = []
    for name in _targets(content, element):
        if runtime.tables.state.get(str(turn), {}).get(name) != 1:
            continue  # already out of view
        lifecycle.end_cell(name, turn, turn=max(turn + 1, current_turn))
        hidden.append(name)
    result = _project_and_save(runtime, history, current_turn)
    print(f"[turn {turn} hidden] {hidden or '(已经不可见)'}")
    return {"ok": True, "turn": turn, "hidden": hidden, **result}


def show_turn(runtime: Any, turn: int, element: str | None = None) -> dict:
    """Append a fresh interval segment so the records are in view again.

    Nothing is recomputed and no earlier segment is edited: the record now
    carries a gap, which reads as "it was here, it left, it is back".
    """

    current_turn = max((int(key) for key in runtime.tables.history), default=0)
    content = _require_turn(runtime, turn)
    history = copy.deepcopy(runtime.tables.history)
    lifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    shown: list[str] = []
    refused: dict[str, str] = {}
    for name in _targets(content, element):
        if runtime.tables.state.get(str(turn), {}).get(name) == 1:
            continue  # already in view
        if (content.get(name) or {}).get(DEAD_KEY) is not None:
            # Killed, and the ledger says so. Reviving it from a button would
            # make delete just a louder hide; the marker has to be removed by
            # hand, which is the whole difference between the two verbs.
            refused[name] = f"已在第 {content[name][DEAD_KEY]} 轮删除，恢复需手动改 history.jsonl"
            continue
        try:
            lifecycle.reopen_cell(name, turn, turn=current_turn)
        except ValueError as exc:
            # A slot can be out of view for two different reasons: the user
            # hid it, or its own declaration says it is finished (think is
            # [born, born] — one turn and gone). Restoring must not override a
            # declaration; that would put the rule back in the ledger, which
            # is what declarations exist to prevent. Report it instead.
            refused[name] = str(exc)
            continue
        shown.append(name)
    result = _project_and_save(runtime, history, current_turn)
    print(f"[turn {turn} shown] {shown or '(没有可恢复的)'}")
    return {"ok": True, "turn": turn, "shown": shown, "refused": refused, **result}


DEAD_KEY = "dead"


def delete_turn(runtime: Any, turn: int, element: str | None = None) -> dict:
    """Kill the records: closed for good, and the killing is itself recorded.

    Deleting does not erase. Erasing would make the ledger lie — the turn did
    happen, the model did answer with it, and a conversation that silently
    loses a turn cannot be audited or replayed. So the record stays and gains
    a ``dead`` marker carrying the turn at which it was killed, exactly the
    way an interval carries when a slot stopped being visible.

    What separates this from hiding is not how much is destroyed but whether
    it can be taken back: ``show`` refuses a dead record. Undoing means going
    into history.jsonl and removing the marker by hand — deliberately harder
    than clicking, because the effect has already happened.
    """

    current_turn = max((int(key) for key in runtime.tables.history), default=0)
    content = _require_turn(runtime, turn)
    history = copy.deepcopy(runtime.tables.history)
    lifecycle = manager["lifecycle.fixed"](
        history, runtime.tables.life_cycle, current_turn=current_turn
    )
    killed = []
    for name in _targets(content, element):
        slot = history[str(turn)].get(name)
        if slot is None or slot.get(DEAD_KEY) is not None:
            continue
        if runtime.tables.state.get(str(turn), {}).get(name) == 1:
            lifecycle.end_cell(name, turn, turn=max(turn + 1, current_turn))
        # Recorded on the slot, not in a side table: whoever reads this
        # ledger later sees the death next to the thing that died.
        slot[DEAD_KEY] = current_turn
        killed.append(name)
    result = _project_and_save(runtime, history, current_turn)
    print(f"[turn {turn} killed at turn {current_turn}] {killed}")
    return {"ok": True, "turn": turn, "deleted": killed, "died_at": current_turn, **result}


ACTIONS = {"hide": hide_turn, "show": show_turn, "delete": delete_turn}


def apply_action(runtime: Any, payload: dict) -> dict:
    action = str(payload.get("action") or "").strip()
    if action not in ACTIONS:
        raise ValueError(f"未知操作 {action!r}；可选：{', '.join(ACTIONS)}")
    raw_turn = payload.get("turn")
    if isinstance(raw_turn, bool) or not isinstance(raw_turn, int):
        raise ValueError("turn 必须是整数轮号")
    element = payload.get("element")
    element = str(element).strip() if element else None
    return ACTIONS[action](runtime, raw_turn, element)


def _self_test() -> None:
    import tempfile
    from pathlib import Path

    from ..dataset.load_config import DEFAULT_CONFIG_PATH, load_config
    from ..manager import runtime as runtime_module

    class Stub:
        def chat(self, context, *, turn_id, **_):
            return f"<think>{turn_id}</think> id：{turn_id}"

        def chat_stream(self, context, *, turn_id, **_):
            yield self.chat(context, turn_id=turn_id)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "runtime.yaml"
        config_path.write_text(
            DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
            .replace("path: ../../task/standard", "path: task", 1)
            .replace("interface: input", "interface: gui", 1),
            encoding="utf-8",
        )
        cfg = load_config(config_path)
        runtime = runtime_module.build_runtime(cfg, provider_instance=Stub())
        runtime.process_turn("first")
        runtime.process_turn("second")
        runtime.process_turn("third")

        assert runtime.tables.state["2"]["user"] == 1
        assert runtime.tables.state["2"]["assistant"] == 1

        # Hiding leaves the words alone and only closes the interval.
        before = runtime.tables.history["2"]["assistant"]["content"]
        hidden = hide_turn(runtime, 2)
        assert hidden["ok"] and "assistant" in hidden["hidden"]
        assert runtime.tables.state["2"]["assistant"] == 0
        assert runtime.tables.state["2"]["user"] == 0
        assert runtime.tables.history["2"]["assistant"]["content"] == before
        assert runtime.tables.state["1"]["user"] == 1  # neighbours untouched
        assert runtime.tables.state["3"]["user"] == 1

        # And it is reversible — for the slots whose declaration still allows
        # it. think is [born, born], so its own rule says it is finished; that
        # refusal is reported rather than overridden.
        shown = show_turn(runtime, 2)
        assert "assistant" in shown["shown"]
        assert "user" in shown["shown"]
        assert "think" in shown["refused"]
        assert runtime.tables.state["2"]["assistant"] == 1
        assert runtime.tables.state["2"]["think"] == 0
        assert runtime.tables.history["2"]["assistant"]["content"] == before

        # One slot at a time: silence the answer, keep the question.
        hide_turn(runtime, 2, "assistant")
        assert runtime.tables.state["2"]["assistant"] == 0
        assert runtime.tables.state["2"]["user"] == 1

        # Deleting keeps the record and marks it dead at the turn it died,
        # so the ledger still accounts for what happened.
        runtime.process_turn("fourth")
        deleted = delete_turn(runtime, 2, "user")
        assert deleted["deleted"] == ["user"]
        assert deleted["died_at"] == 4
        slot = runtime.tables.history["2"]["user"]
        assert slot[DEAD_KEY] == 4
        assert slot["content"] == "second"  # the words are still on record
        assert runtime.tables.state["2"]["user"] == 0
        assert "3" in runtime.tables.history  # neighbours keep their numbers

        # And a dead record does not come back from the UI.
        again = show_turn(runtime, 2, "user")
        assert again["shown"] == []
        assert "user" in again["refused"]
        assert runtime.tables.state["2"]["user"] == 0

        for bad in ({"action": "nope", "turn": 1}, {"action": "hide", "turn": "1"},
                    {"action": "hide", "turn": 99}):
            try:
                apply_action(runtime, bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"应当拒绝：{bad}")


if __name__ == "__main__":
    _self_test()
    print("saver.turn_control: ok")
