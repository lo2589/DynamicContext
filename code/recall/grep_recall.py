"""Lexical recall over the history table — the simplest possible recall.

Recall is an act of connecting, not a cache lookup. Being present in the
context is not the same as being recognized as relevant to what was just
asked — a pinned fact can sit in view for the whole conversation and still
go unused. So recall searches the whole declared evidence range, including
slots that are still active: surfacing one again under the current question
is what makes the link, and that link is the thing recall supplies.

What it may search is declared (search_fields); how far back is the whole
history. No embeddings, no external store, no index: history.jsonl is the
only source of truth, and grepping it needs nothing else.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..registry import recall


DEFAULT_TOP_K = 3
DEFAULT_MIN_SCORE = 1

_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_CHAR = re.compile(r"[㐀-鿿]")


def lexical_tokens(text: str) -> set[str]:
    """Tokens for overlap scoring, in a way that works for both scripts.

    ASCII gets whitespace/punctuation words. CJK has no word boundaries, so
    it gets character bigrams — the standard cheap substitute for
    segmentation in lexical retrieval, and enough to make "花生" match
    "花生酱" without pulling in every sentence that merely shares one
    common character.
    """

    source = str(text or "").lower()
    tokens = set(_ASCII_WORD.findall(source))
    cjk = _CJK_CHAR.findall(source)
    cjk_run = "".join(cjk)
    tokens.update(cjk_run[index : index + 2] for index in range(len(cjk_run) - 1))
    return tokens


def _recall_settings(cfg: Any) -> tuple[int, int]:
    if cfg is None:
        return DEFAULT_TOP_K, DEFAULT_MIN_SCORE
    section = cfg.to_dict().get("recall")
    section = section if isinstance(section, dict) else {}
    top_k = section.get("top_k") or DEFAULT_TOP_K
    min_score = section.get("min_score") or DEFAULT_MIN_SCORE
    return int(top_k), int(min_score)


def _query_text(history: dict, turn_id: int, query_element: str) -> str:
    """This turn's input is the query — already written into history by the
    time recall runs. Which slot holds it is told to us by the channel that
    wrote it, never guessed from a name."""

    if not query_element:
        return ""
    slot = history.get(str(turn_id), {}).get(query_element) or {}
    return str(slot.get("content") or "")


@recall("grep")
def grep_history(
    *,
    history: Optional[dict] = None,
    state: Optional[dict] = None,
    turn_id: int = 0,
    cfg: Any = None,
    query_element: str = "",
    search_fields: tuple[str, ...] = (),
    **_: Any,
) -> list[str]:
    """Pull back the best-matching retired slots for this turn's query.

    Returns plain content lines, not chat messages: the caller writes them
    into history as a slot like any other channel's output, and the shared
    element-to-role projection turns them into a message from there.
    """

    history = history or {}
    if not search_fields:
        return []
    searchable = set(search_fields)
    top_k, min_score = _recall_settings(cfg)
    query = lexical_tokens(_query_text(history, turn_id, query_element))
    if not query or top_k < 1:
        return []

    scored: list[tuple[int, int, str, str]] = []
    for stored_turn, content in history.items():
        if int(stored_turn) >= turn_id:
            continue  # the current turn is already in context verbatim
        for element, slot in content.items():
            if element not in searchable:
                continue  # not declared as searchable evidence
            text = str(slot.get("content") or "")
            score = len(query & lexical_tokens(text))
            if score >= min_score:
                scored.append((score, int(stored_turn), element, text))

    # Highest score first; ties broken by recency, so an older and a newer
    # slot that match equally well surface the newer one.
    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [
        f"第{stored_turn}轮 {element}：{text}"
        for _, stored_turn, element, text in scored[:top_k]
    ]


def _self_test() -> None:
    assert lexical_tokens("hello WORLD") == {"hello", "world"}
    assert "花生" in lexical_tokens("孩子对花生过敏")
    assert lexical_tokens("") == set()

    # Turn 1 was retired (compacted away); turn 2 is still authoritative.
    history = {
        "1": {
            "user": {"content": "孩子对花生过敏，千万不能吃"},
            "assistant": {"content": "好的，我记住了"},
        },
        "2": {"user": {"content": "今天天气不错"}},
        "3": {"user": {"content": "想给孩子做花生酱三明治"}},
    }
    state = {
        "1": {"user": 0, "assistant": 0},  # retired by compaction
        "2": {"user": 1},  # still active
        "3": {"user": 1},
    }
    recalled = grep_history(history=history, state=state, turn_id=3, query_element="user", search_fields=("user","assistant"))
    assert len(recalled) == 1, recalled
    assert "花生过敏" in recalled[0]
    assert recalled[0].startswith("第1轮 user：")

    # An active slot is recalled too: being in context is not the same as
    # being connected to the question just asked.
    all_active = {turn: {element: 1 for element in content} for turn, content in history.items()}
    assert grep_history(history=history, state=all_active, turn_id=3,
                        query_element="user",
                        search_fields=("user", "assistant")) == recalled

    # No lexical overlap -> nothing pulled back.
    assert grep_history(history=history, state=state, turn_id=2, query_element="user", search_fields=("user","assistant")) == []
    # No query slot named -> nothing to search with.
    assert grep_history(history=history, state=state, turn_id=3,
                        search_fields=("user",)) == []
    # No searchable fields declared -> nothing is searched at all.
    assert grep_history(history=history, state=state, turn_id=3,
                        query_element="user") == []
    # A retired slot outside search_fields is never returned, however well it
    # matches — which is what keeps discarded reasoning out of recall.
    thinky = {"1": {"think": {"content": "孩子对花生过敏的推理"}},
              "2": {"user": {"content": "花生"}}}
    thinky_state = {"1": {"think": 0}, "2": {"user": 1}}
    assert grep_history(history=thinky, state=thinky_state, turn_id=2,
                        query_element="user", search_fields=("user", "assistant")) == []
    assert grep_history(history=thinky, state=thinky_state, turn_id=2,
                        query_element="user", search_fields=("think",)) != []

    # top_k caps the result even when more slots match.
    wide_history = {
        str(turn): {"user": {"content": "花生过敏提醒"}} for turn in range(1, 6)
    }
    wide_history["6"] = {"user": {"content": "花生怎么保存"}}
    wide_state = {turn: {"user": 0} for turn in wide_history}
    wide_state["6"] = {"user": 1}
    capped = grep_history(history=wide_history, state=wide_state, turn_id=6, query_element="user", search_fields=("user",))
    assert len(capped) == DEFAULT_TOP_K
    # Ties break toward recency: turns 5, 4, 3 rather than 1, 2, 3.
    assert capped[0].startswith("第5轮")


if __name__ == "__main__":
    _self_test()
    print("recall.grep_recall: ok")
