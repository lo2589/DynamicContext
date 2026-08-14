"""Project fixed history/state tables into an OpenAI message snapshot."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..registry import manager


# The roles an OpenAI-shaped chat API accepts. This is the wire protocol,
# not a list of this project's slot names — an element carries a protocol
# role only because it was declared to, or because it is literally named
# after one.
PROTOCOL_ROLES = frozenset({"system", "user", "assistant"})
FALLBACK_ROLE = "system"


def _message_for_element(
    element: str,
    content: str,
    roles: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Render one slot as a chat message.

    Which role a slot speaks in is declared (context.roles, or an answer
    output's own ``role``), never inferred from a hardcoded list of element
    names. An element named after a protocol role needs no declaration; any
    other undeclared element is preserved verbatim under a valid role,
    labelled with its own name so nothing silently loses its identity.
    """

    declared = (roles or {}).get(element)
    if declared:
        return {"role": str(declared), "content": content}
    if element in PROTOCOL_ROLES:
        return {"role": element, "content": content}
    return {"role": FALLBACK_ROLE, "content": f"[{element}]\n{content}"}


@manager("context.select.none")
def select_context_without_extra_filter(
    history: dict[str, dict[str, dict[str, Any]]],
    state: dict[str, dict[str, int]],
    *,
    through_turn: int | None = None,
    roles: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Select every currently active raw element; no extra recall/filtering."""

    messages: list[dict[str, str]] = []
    for turn_id in sorted(history, key=int):
        if through_turn is not None and int(turn_id) > through_turn:
            continue
        for element, slot in history[turn_id].items():
            if state.get(turn_id, {}).get(element, 0) != 1:
                continue
            messages.append(
                _message_for_element(element, str(slot.get("content") or ""), roles)
            )
    return messages


def _self_test() -> None:
    history = {
        "0": {"system": {"content": "s"}},
        "1": {
            "goal": {"content": "g"},
            "user": {"content": "u"},
            "think": {"content": "hidden"},
            "assistant": {"content": "a"},
        },
    }
    state = {
        "0": {"system": 1},
        "1": {"goal": 1, "user": 1, "think": 0, "assistant": 1},
    }
    # goal is this project's own slot name, so it only speaks as "user"
    # because the config says so; system/user/assistant need no declaration
    # because they are named after the protocol's own roles.
    roles = {"goal": "user"}
    assert select_context_without_extra_filter(history, state, roles=roles) == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "g"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]

    # Undeclared elements keep their identity rather than being guessed at.
    assert select_context_without_extra_filter(history, state) == [
        {"role": "system", "content": "s"},
        {"role": "system", "content": "[goal]\ng"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
    ]

    # A declaration overrides even a protocol-named element.
    assert _message_for_element("user", "u", {"user": "system"}) == {
        "role": "system",
        "content": "u",
    }


if __name__ == "__main__":
    _self_test()
    print("manager.context: ok")
