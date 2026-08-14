"""Normalize provider output into independent history elements."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..registry import provider


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"
PIN_OPEN = "<pin>"
PIN_CLOSE = "</pin>"


def extract_pins(text: str) -> tuple[str, list[str]]:
    """Pull every <pin>...</pin> block out of raw provider text, in the
    order they appear. A truncated trailing <pin> (generation cut off
    mid-tag) still yields whatever was captured — same truncation-safety
    split_embedded_think already gives <think>."""

    source = str(text or "")
    lowered = source.lower()
    remainder_parts: list[str] = []
    pins: list[str] = []
    cursor = 0

    while True:
        begin = lowered.find(PIN_OPEN, cursor)
        if begin < 0:
            remainder_parts.append(source[cursor:])
            break
        remainder_parts.append(source[cursor:begin])
        pin_begin = begin + len(PIN_OPEN)
        end = lowered.find(PIN_CLOSE, pin_begin)
        if end < 0:
            pin_text = source[pin_begin:].strip()
            if pin_text:
                pins.append(pin_text)
            break
        pin_text = source[pin_begin:end].strip()
        if pin_text:
            pins.append(pin_text)
        cursor = end + len(PIN_CLOSE)

    return "".join(remainder_parts), pins


def split_embedded_think(text: str) -> tuple[str, str]:
    """Everything up to the last ``</think>`` is reasoning; what follows is the answer.

    The close tag is what matters. Some providers drop, truncate, or duplicate the
    opening ``<think>`` under streaming, and a closing tag marks where the model
    itself drew the line between thinking and answering — an answer is never
    supposed to contain one. Splitting on an opening tag that has to be found
    first is what let text after a garbled or missing open tag fall through into
    the answer, so the close tag is searched for first and alone decides the cut.

    A generation can also stop mid-thought, before any closing tag exists at all
    (an interrupted stream, or a truncated response). That case has no close tag
    to cut on, so it falls back to the open tag: whatever follows the last one
    is unfinished reasoning, and there is no answer yet to report.
    """

    source = str(text or "")
    lowered = source.lower()
    end = lowered.rfind(THINK_CLOSE)
    if end < 0:
        start = lowered.rfind(THINK_OPEN)
        if start < 0:
            return source.strip(), ""
        return "", source[start + len(THINK_OPEN):].strip()

    before = source[:end]
    after = source[end + len(THINK_CLOSE):]
    # Everything before the last close tag is reasoning, including any stray
    # open or close tags a duplicated/garbled stream left scattered inside it —
    # those are noise from the same malformation, not content to keep.
    think = re.sub(f"{re.escape(THINK_OPEN)}|{re.escape(THINK_CLOSE)}", "", before, flags=re.I).strip()
    return after.strip(), think


TAG_PATTERN = re.compile(r"<([A-Za-z][A-Za-z0-9_]*)>(.*?)</\1>", re.DOTALL)
RESERVED_TAGS = frozenset({"think", "pin"})


def extract_tags(text: str, *, reserved: frozenset[str] = RESERVED_TAGS) -> tuple[str, dict[str, str]]:
    """Pull every ``<name>...</name>`` block out, keyed by its own tag name.

    A parser that knows one tag can only ever produce the slot it was written
    for. This produces whatever the model was asked to produce: the tag names
    the answer carries become the element names the turn writes, so a new
    output channel is a line of prompt plus a line of life_cycle, not a new
    parser.

    ``reserved`` names are left in place for the parsers that own them. A tag
    appearing more than once keeps the last occurrence.
    """

    source = str(text or "")
    found: dict[str, str] = {}

    def take(match: "re.Match[str]") -> str:
        name = match.group(1).lower()
        if name in reserved:
            return match.group(0)
        body = match.group(2).strip()
        if body:
            found[name] = body
        return ""

    return TAG_PATTERN.sub(take, source), found


@dataclass(frozen=True)
class ParsedAnswer:
    assistant: str
    think: str = ""
    pins: tuple[str, ...] = ()
    raw: str = ""
    tagged: Mapping[str, str] = field(default_factory=dict)

    def elements(self) -> dict[str, str]:
        # pins are intentionally not here: each needs a dynamically
        # allocated element name (pin_{origin}_{id}), which this fixed-
        # key-per-role shape can't express — the caller walks .pins
        # directly (see runtime.py's call_and_parse).
        values: dict[str, str] = {}
        if self.think:
            values["think"] = self.think
        # Whatever tags the answer carried, under their own names. Their
        # life_cycle is declared like any other element's; undeclared is an
        # error the same way it is everywhere else.
        values.update(self.tagged)
        # An interrupted generation can legitimately finish with think only.
        # Keep an assistant slot as the marker that this turn was completed.
        values["assistant"] = self.assistant
        return values


@provider("answer.parse.normal")
def parse_normal_answer(raw_answer: str) -> ParsedAnswer:
    stripped, pins = extract_pins(str(raw_answer or ""))
    assistant, think = split_embedded_think(stripped)
    return ParsedAnswer(
        assistant=assistant, think=think, pins=tuple(pins), raw=str(raw_answer or "")
    )


@provider("answer.parse.tagged")
def parse_tagged_answer(raw_answer: str) -> ParsedAnswer:
    """``normal``, plus every other tag the answer carries becomes a slot."""

    stripped, pins = extract_pins(str(raw_answer or ""))
    assistant, think = split_embedded_think(stripped)
    assistant, tagged = extract_tags(assistant)
    return ParsedAnswer(
        assistant=assistant.strip(),
        think=think,
        pins=tuple(pins),
        raw=str(raw_answer or ""),
        tagged=tagged,
    )


def _self_test() -> None:
    parsed = parse_normal_answer("<think>reason</think>answer")
    assert parsed.think == "reason"
    assert parsed.assistant == "answer"
    interrupted = parse_normal_answer("<think>unfinished")
    assert interrupted.think == "unfinished"
    assert interrupted.assistant == ""
    assert interrupted.elements()["assistant"] == ""

    pinned = parse_normal_answer(
        "<think>noting an allergy</think>"
        "<pin>2026-07-20 家里 妈妈说示例事实一</pin>"
        "好的，我记住了。"
        "<pin>2026-07-20 家里 晚饭吃了面条</pin>"
    )
    assert pinned.think == "noting an allergy"
    assert pinned.assistant == "好的，我记住了。"
    assert pinned.pins == (
        "2026-07-20 家里 妈妈说示例事实一",
        "2026-07-20 家里 晚饭吃了面条",
    )
    assert "pins" not in pinned.elements()

    no_pins = parse_normal_answer("<think>r</think>answer")
    assert no_pins.pins == ()

    # A tag the answer carries becomes a slot under its own name; the parser
    # was never told what "card" is.
    carded = parse_tagged_answer(
        "<think>r</think><card>Name: Sarah | Age: 38</card>Noted."
    )
    assert carded.think == "r"
    assert carded.assistant == "Noted."
    assert carded.elements()["card"] == "Name: Sarah | Age: 38"
    assert parse_normal_answer("<card>x</card>y").elements().get("card") is None

    truncated_pin = parse_normal_answer("answer text <pin>unfinished pin")
    assert truncated_pin.assistant == "answer text"
    assert truncated_pin.pins == ("unfinished pin",)


if __name__ == "__main__":
    _self_test()
    print("provider.answer_parser: ok")
