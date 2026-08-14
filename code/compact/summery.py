####传统的compat方法，
##
"""Summary compaction: fold a stretch of raw turns into one written summary.

Two separate policy axes live here:

  the compressor  — how a window of raw turns becomes one summary
  retention       — which already-written summaries stay visible afterwards

A summary is computed once, for the stretch it covers, and never fed back
through the model. Retention therefore never recomputes anything; it only
decides visibility, and the runtime carries that out by closing or appending
range segments on slots that already exist.

Every strategy is an ordinary registered function. The runtime resolves the
configured name to a handle once at build time and passes the handle down,
so no call site ever branches on a policy name.
"""

from __future__ import annotations

from typing import Any

from ..registry import compact


DEFAULT_RETENTION = "latest_only"


@compact("summary")
def compute_summary(
    runtime: Any,
    transcript: str,
    prompt_template: str,
    *,
    turn_id: int,
) -> str:
    """Call the model once to fold a raw-turn window into a short summary."""

    prompt = prompt_template.format(content=transcript)
    raw_answer = runtime.chat(
        runtime.provider,
        [{"role": "user", "content": prompt}],
        turn_id=turn_id,
        on_chunk=None,
        tools=runtime.tools,
        search=runtime.search,
    )
    parsed = runtime.answer_parser(raw_answer)
    return parsed.assistant.strip()


@compact("retain.latest_only")
def retain_latest_only(*, anchors: list[int], **_: Any) -> set[int]:
    """Only the newest summary stays visible; each one supersedes the last."""
    return {max(anchors)} if anchors else set()


@compact("retain.keep_all")
def retain_keep_all(*, anchors: list[int], **_: Any) -> set[int]:
    """Every summary ever written stays visible."""
    return set(anchors)


@compact("retain.last_k")
def retain_last_k(*, anchors: list[int], k: int = 3, **_: Any) -> set[int]:
    """The k most recent summaries stay visible."""
    if k < 1:
        raise ValueError("retain.last_k 的 k 必须是正整数")
    return set(sorted(anchors)[-k:])


@compact("retain.equidistant")
def retain_equidistant(
    *, anchors: list[int], current_turn: int, k: int = 3, **_: Any
) -> set[int]:
    """Keep k summaries spread evenly over the compressed span.

    Targets sit at current_turn*i/k for i in 1..k — so k=3 at turn m keeps
    the summaries nearest 1/3 m, 2/3 m and m. Distant history thins out
    instead of vanishing, which a purely recency-based policy cannot do.
    Ties prefer the later anchor; fewer than k survive when two targets
    resolve to the same summary.
    """
    if k < 1:
        raise ValueError("retain.equidistant 的 k 必须是正整数")
    if not anchors:
        return set()
    kept = {
        min(anchors, key=lambda anchor: (abs(anchor - current_turn * index / k), -anchor))
        for index in range(1, k + 1)
    }
    # The newest summary is always one of them: it covers the stretch nothing
    # else does. The final division point lands on current_turn, so the
    # nearest-anchor rule already picks it — stated outright so no unusual
    # k or spacing can drop the one summary that must be there.
    kept.add(max(anchors))
    return kept


def _self_test() -> None:
    anchors = [5, 10, 15, 20, 25, 30]
    assert retain_latest_only(anchors=anchors) == {30}
    assert retain_latest_only(anchors=[]) == set()
    assert retain_keep_all(anchors=anchors) == set(anchors)
    assert retain_last_k(anchors=anchors, k=2) == {25, 30}
    assert retain_last_k(anchors=anchors, k=99) == set(anchors)
    assert retain_equidistant(anchors=anchors, current_turn=30, k=3) == {10, 20, 30}
    assert retain_equidistant(anchors=anchors, current_turn=30, k=2) == {15, 30}
    assert retain_equidistant(anchors=[], current_turn=9) == set()
    # Fewer anchors than k is not an error; one anchor can serve two targets.
    assert retain_equidistant(anchors=[7], current_turn=30, k=3) == {7}
    # The newest is always kept, whatever the division points land on.
    for k in range(1, 6):
        assert 30 in retain_equidistant(anchors=anchors, current_turn=30, k=k)
    for bad in (0, -1):
        for strategy in (retain_last_k, retain_equidistant):
            try:
                strategy(anchors=anchors, current_turn=30, k=bad)
            except ValueError:
                pass
            else:
                raise AssertionError(f"{strategy.__name__} 接受了非法 k={bad}")

    class _Parsed:
        assistant = "  折叠后的摘要  "

    class _Runtime:
        provider = object()
        tools = None
        search = None

        def chat(self, _provider, context, **kwargs):
            assert context == [{"role": "user", "content": "请压缩：原文"}]
            assert kwargs["turn_id"] == 7
            return "raw"

        @staticmethod
        def answer_parser(_raw):
            return _Parsed()

    assert compute_summary(_Runtime(), "原文", "请压缩：{content}", turn_id=7) == "折叠后的摘要"


if __name__ == "__main__":
    _self_test()
    print("compact.summery: ok")
