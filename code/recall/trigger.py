"""When recall fires — an axis of its own, separate from which recall runs.

``recall.type`` picks *how* to look things up (grep is one such type);
``recall.trigger`` picks *when* to bother looking at all. Keeping them apart
means a pattern trigger works the same whether the lookup behind it is grep,
an embedding store, or anything registered later.

Each trigger is an ordinary registered function resolved to a handle once at
build time, so the call site never branches on the trigger's name.
"""

from __future__ import annotations

import re
from typing import Any

from ..registry import recall




@recall("trigger.always")
def trigger_always(**_: Any) -> bool:
    """Look something up on every turn."""
    return True


@recall("trigger.never")
def trigger_never(**_: Any) -> bool:
    """Never look anything up; recall stays configured but idle."""
    return False


@recall("trigger.manual")
def trigger_manual(**_: Any) -> bool:
    """Reserved for an explicit user command; never fires on its own."""
    return False


@recall("trigger.pattern")
def trigger_pattern(*, query: str = "", pattern: str = "", **_: Any) -> bool:
    """Fire only when this turn matches a configured regex.

    Which words mark a turn as reaching backwards depends entirely on the
    language and the task, so the regex is required config — there is no
    default here to quietly impose one language's vocabulary on every task.
    """
    if not pattern:
        raise ValueError(
            "recall.trigger 选用 pattern 时必须声明 pattern："
            "触发词表依赖语言和任务，属于配置"
        )
    if not query:
        return False
    try:
        return re.search(pattern, query) is not None
    except re.error as exc:
        raise ValueError(f"recall.trigger.pattern 不是合法正则: {exc}") from exc


def _self_test() -> None:
    assert trigger_always() is True
    assert trigger_never() is False
    assert trigger_manual() is False

    # The pattern is config, in whatever language the task speaks.
    backward = r"回忆|想起|记得|之前|说过|想想"
    for query in ("你还记得我之前说过什么吗", "回忆一下上次的结论", "让我想想"):
        assert trigger_pattern(query=query, pattern=backward) is True, query
    for query in ("今天天气怎么样", "帮我写个函数", "这个怎么读"):
        assert trigger_pattern(query=query, pattern=backward) is False, query
    assert trigger_pattern(query="", pattern=backward) is False

    assert trigger_pattern(query="remind me what we said", pattern=r"remind|recall") is True
    assert trigger_pattern(query="hello", pattern=r"remind|recall") is False

    # No default vocabulary is imposed: the regex must be declared.
    try:
        trigger_pattern(query="回忆一下")
    except ValueError:
        pass
    else:
        raise AssertionError("未声明 pattern 应该报错")

    try:
        trigger_pattern(query="x", pattern="[unclosed")
    except ValueError:
        pass
    else:
        raise AssertionError("非法正则没有报错")


if __name__ == "__main__":
    _self_test()
    print("recall.trigger: ok")
