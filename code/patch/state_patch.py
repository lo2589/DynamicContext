"""Row and cell lifecycle-state patches from the engine reference."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import json
from math import isinf
from pathlib import Path
import re
from typing import Any, Optional, Union

from ..dataset.table_manager import ColumnId, RowId
from ..lifecycle.span import INF, Remain, validate_remain
from ..registry import patch as patch_registry


class RowPatchMode(str, Enum):
    """一行生命周期规则的三种修改方式。

    FORWARD：旧槽位不变，新规则只管 patch 后诞生的槽位。
    FORCE_REFRESH：按旧槽位各自的诞生轮次，用新规则重算结束轮次。
    RESET_FROM_NOW：本轮结束全部旧槽位；新规则只管之后的新槽位。

    ADD_ROW 只负责新增槽位类型，不属于上述三种修改方式。
    """

    FORWARD = "forward"
    FORCE_REFRESH = "force_refresh"
    RESET_FROM_NOW = "reset_from_now"
    ADD_ROW = "add_row"


class ContentPatchMode(str, Enum):
    REMAIN = "remain"
    ROLL = "roll"
    # Ends whatever is currently active in the element (like ROLL), then
    # starts one cell that does not expire on its own (like REMAIN with
    # turns=INF) -- the previous value becomes invalid immediately, and the
    # new one stands until another patch (of any mode) replaces it.
    REFRESH = "refresh"


@dataclass(frozen=True)
class ContentPatch:
    creator: str
    element: str
    content: str
    mode: ContentPatchMode
    turns: Remain
    created_turn: Optional[int] = None


@patch_registry("content.for_turn")
def content_patch_for_turn(
    content_patch: ContentPatch,
    turn: int,
) -> Optional[ContentPatch]:
    """Materialize one patch occurrence for a concrete dialogue turn."""

    if content_patch.created_turn is None:
        raise ValueError("ContentPatch缺少created_turn")
    born = content_patch.created_turn
    if content_patch.mode in (ContentPatchMode.REMAIN, ContentPatchMode.REFRESH):
        return content_patch if turn == born else None
    if born <= turn < born + content_patch.turns:
        # A rolling value is a new one-turn cell on every covered turn.
        return replace(content_patch, created_turn=turn, turns=1)
    return None


@patch_registry("content.pending_after")
def content_patch_pending_after(
    content_patch: ContentPatch,
    turn: int,
) -> bool:
    """Whether this source patch still has an occurrence after ``turn``."""

    if content_patch.created_turn is None:
        raise ValueError("ContentPatch缺少created_turn")
    last_turn = (
        content_patch.created_turn
        if content_patch.mode in (ContentPatchMode.REMAIN, ContentPatchMode.REFRESH)
        else content_patch.created_turn + content_patch.turns - 1
    )
    return turn < last_turn


@patch_registry("create")
def create_content_patch(
    *,
    creator: str,
    element: str,
    content: str,
    mode: Union[str, ContentPatchMode],
    turns: int,
    created_turn: Optional[int] = None,
) -> ContentPatch:
    """The shared creation entry for input, GUI, or later interfaces."""
    normalized_creator = creator.strip()
    normalized_element = element.strip().lstrip("/")
    normalized_content = content.strip()
    normalized_mode = ContentPatchMode(mode)
    if not normalized_creator:
        raise ValueError("patch creator不能为空")
    if not normalized_element:
        raise ValueError("patch element不能为空")
    if not normalized_content:
        raise ValueError("patch content不能为空")
    try:
        validate_remain(turns)
    except ValueError:
        raise ValueError("patch turns必须是正整数或none(无限期)") from None
    if created_turn is not None and (
        isinstance(created_turn, bool)
        or not isinstance(created_turn, int)
        or created_turn < 1
    ):
        raise ValueError("patch created_turn必须是正整数")
    return ContentPatch(
        creator=normalized_creator,
        element=normalized_element,
        content=normalized_content,
        mode=normalized_mode,
        turns=turns,
        created_turn=created_turn,
    )


PATCH_INPUT_PATTERN = re.compile(
    r"^/(?P<element>\S+)\s+(?P<content>.+)\s+"
    r"(?P<mode>remain|remian|roll|refresh)\s+(?P<turns>[1-9]\d*|none)$"
)
INLINE_PATCH_INPUT_PATTERN = re.compile(
    r"^(?:(?P<user>.+?)\s+)?/(?P<element>\S+)\s+(?P<content>.+)\s+"
    r"(?P<mode>remain|remian|roll|refresh)\s+(?P<turns>[1-9]\d*|none)$"
)
PATCH_FIRST_INPUT_PATTERN = re.compile(
    r"^/(?P<element>\S+)\s+(?P<content>.+?)\s+"
    r"(?P<mode>remain|remian|roll|refresh)\s+(?P<turns>[1-9]\d*|none)"
    r"(?:\s+(?P<user>.+))?$"
)


def _patch_from_match(match: re.Match[str], creator: str) -> ContentPatch:
    mode = match.group("mode")
    if mode == "remian":
        mode = "remain"
    turns_text = match.group("turns")
    turns: Remain = INF if turns_text == "none" else int(turns_text)
    return patch_registry["create"](
        creator=creator,
        element=match.group("element"),
        content=match.group("content"),
        mode=mode,
        turns=turns,
    )


@patch_registry("input.parse")
def parse_patch_input(
    user_input: str,
    *,
    creator: str = "user",
) -> Optional[ContentPatch]:
    """Parse ``/goal xxx remain 5`` or ``/goal xxx roll 5``."""
    normalized_input = user_input.strip()
    if not normalized_input.startswith("/"):
        return None
    match = PATCH_INPUT_PATTERN.fullmatch(normalized_input)
    if match is None:
        raise ValueError(
            "patch格式必须是 /元素 内容 remain|roll 正整数，"
            "或 /元素 内容 refresh none（内容在模式前面，不是后面）"
        )
    return _patch_from_match(match, creator)


@patch_registry("input.extract")
def extract_patch_input(
    user_input: str,
    *,
    creator: str = "user",
) -> tuple[Optional[str], Optional[ContentPatch]]:
    """Extract one optional trailing patch from the same user input line."""

    normalized_input = user_input.strip()
    match = INLINE_PATCH_INPUT_PATTERN.fullmatch(normalized_input)
    if match is None:
        match = PATCH_FIRST_INPUT_PATTERN.fullmatch(normalized_input)
    if match is None:
        if normalized_input.startswith("/"):
            # Keep strict validation for a line intended to be a patch command.
            return None, parse_patch_input(normalized_input, creator=creator)
        return normalized_input, None
    user_text = match.group("user")
    return (
        user_text.strip() if user_text and user_text.strip() else None,
        _patch_from_match(match, creator),
    )


@patch_registry("yaml.write")
def write_content_patch_to_yaml(
    cfg: Any,
    content_patch: ContentPatch,
    *,
    created_turn: int,
) -> ContentPatch:
    """Insert one parsed dialogue patch directly under context.patches."""
    stored_patch = replace(content_patch, created_turn=created_turn)
    yaml_path = Path(cfg.source_path)
    lines = yaml_path.read_text(encoding="utf-8").splitlines()
    patches_line = _find_context_patches_line(lines)
    patch_count = sum(
        line.startswith(f"    turn_{created_turn}_") for line in lines
    )
    patch_id = f"turn_{created_turn}_{patch_count + 1}"
    block = _content_patch_yaml(patch_id, stored_patch)
    if lines[patches_line].strip() == "patches: {}":
        lines[patches_line] = "  patches:"
        insert_at = patches_line + 1
    else:
        insert_at = patches_line + 1
        while insert_at < len(lines):
            line = lines[insert_at]
            if line.strip() and not line.startswith("    "):
                break
            insert_at += 1
    lines[insert_at:insert_at] = block
    yaml_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return stored_patch


def _find_context_patches_line(lines: list[str]) -> int:
    in_context = False
    for index, line in enumerate(lines):
        if line and not line.startswith(" "):
            in_context = line.strip() == "context:"
            continue
        if in_context and line.startswith("  patches:"):
            return index
    raise ValueError("YAML缺少context.patches")


def _content_patch_yaml(
    patch_id: str,
    content_patch: ContentPatch,
) -> list[str]:
    quote = lambda value: json.dumps(value, ensure_ascii=False)
    turns = content_patch.turns
    turns_text = "none" if isinstance(turns, float) and isinf(turns) else str(turns)
    return [
        f"    {patch_id}:",
        f"      creator: {quote(content_patch.creator)}",
        f"      element: {quote(content_patch.element)}",
        f"      content: {quote(content_patch.content)}",
        f"      mode: {quote(content_patch.mode.value)}",
        f"      turns: {turns_text}",
        f"      created_turn: {content_patch.created_turn}",
    ]


@dataclass(frozen=True)
class RowPatch:
    row_id: RowId
    mode: RowPatchMode
    remain: Union[int, float]

@dataclass(frozen=True)
class CellPatch:
    row_id: RowId
    column_id: ColumnId
    remain: Union[int, float]


Patch = Union[RowPatch, CellPatch]


@patch_registry("forward")
def apply_forward(lifecycle: Any, patch: RowPatch, turn: int) -> None:
    """旧槽位不变，新规则只管 patch 后诞生的槽位。"""
    lifecycle.validate_remain(patch.remain)
    if patch.row_id not in lifecycle.state.current_rule:
        raise KeyError(f"row_id={patch.row_id!r} 不存在")
    lifecycle.state.current_rule[patch.row_id] = patch.remain


@patch_registry("force_refresh")
def apply_force_refresh(lifecycle: Any, patch: RowPatch, turn: int) -> None:
    """按各旧槽位的诞生轮次，用新规则重算结束轮次。"""
    lifecycle.validate_remain(patch.remain)
    if patch.row_id not in lifecycle.state.current_rule:
        raise KeyError(f"row_id={patch.row_id!r} 不存在")
    lifecycle.state.current_rule[patch.row_id] = patch.remain
    active = list(
        lifecycle.state.active_finite[patch.row_id]
        | lifecycle.state.active_permanent[patch.row_id]
    )
    for column_id in active:
        born_turn = lifecycle.tables.column_meta[column_id].created_turn
        new_end = lifecycle.calculate_end_turn(born_turn, patch.remain)
        if not (isinstance(new_end, float) and isinf(new_end)) and new_end <= turn:
            lifecycle.end_cell(patch.row_id, column_id, turn=turn)
        else:
            lifecycle.start_cell(
                patch.row_id,
                column_id,
                remain=patch.remain,
                born_turn=born_turn,
            )


@patch_registry("reset_from_now")
def apply_reset_from_now(lifecycle: Any, patch: RowPatch, turn: int) -> None:
    """本轮结束全部旧槽位，新规则只管之后的新槽位。"""
    lifecycle.validate_remain(patch.remain)
    if patch.row_id not in lifecycle.state.current_rule:
        raise KeyError(f"row_id={patch.row_id!r} 不存在")
    lifecycle.state.current_rule[patch.row_id] = patch.remain
    active = list(
        lifecycle.state.active_finite[patch.row_id]
        | lifecycle.state.active_permanent[patch.row_id]
    )
    for column_id in active:
        lifecycle.end_cell(patch.row_id, column_id, turn=turn)


@patch_registry("add_row")
def apply_add_row(lifecycle: Any, patch: RowPatch, turn: int) -> None:
    """新增槽位类型；它不属于三种生命周期修改方式。"""
    lifecycle.add_row(patch.row_id, patch.remain)


@patch_registry("row")
def apply_row_patch(lifecycle: Any, patch: RowPatch, turn: int) -> None:
    """按 mode 调用对应的独立函数。"""
    handler = patch_registry[patch.mode.value]
    handler(lifecycle, patch, turn)


@patch_registry("cell")
def apply_cell_patch(lifecycle: Any, patch: CellPatch, turn: int) -> bool:
    """只重算用户指定的一个旧槽位。"""
    lifecycle.validate_remain(patch.remain)
    if patch.row_id not in lifecycle.state.current_rule:
        raise KeyError(f"row_id={patch.row_id!r} 不存在")
    if patch.column_id not in lifecycle.tables.column_meta:
        raise KeyError(f"column_id={patch.column_id!r} 不存在")
    if not lifecycle.tables.get("CURRENT", patch.row_id, patch.column_id):
        return False

    born_turn = lifecycle.tables.column_meta[patch.column_id].created_turn
    new_end = lifecycle.calculate_end_turn(born_turn, patch.remain)
    if not (isinstance(new_end, float) and isinf(new_end)) and new_end <= turn:
        lifecycle.end_cell(patch.row_id, patch.column_id, turn=turn)
    else:
        lifecycle.start_cell(
            patch.row_id,
            patch.column_id,
            remain=patch.remain,
            born_turn=born_turn,
        )
    return True


def _self_test() -> None:
    row_patch = RowPatch("goal", RowPatchMode.FORWARD, 2)
    cell_patch = CellPatch("goal", 4, 1)
    assert row_patch.mode == RowPatchMode.FORWARD
    assert cell_patch.column_id == 4
    user, content_patch = extract_patch_input(
        "今天天气不好 /goal 翻译文本 remain 5"
    )
    assert user == "今天天气不好"
    assert content_patch is not None
    assert content_patch.element == "goal"
    assert content_patch.content == "翻译文本"
    assert content_patch.turns == 5
    rolling = replace(content_patch, mode=ContentPatchMode.ROLL, created_turn=4, turns=3)
    assert content_patch_for_turn(rolling, 3) is None
    assert content_patch_for_turn(rolling, 4) == replace(
        rolling, created_turn=4, turns=1
    )
    assert content_patch_for_turn(rolling, 5) == replace(
        rolling, created_turn=5, turns=1
    )
    assert content_patch_for_turn(rolling, 6) == replace(
        rolling, created_turn=6, turns=1
    )
    assert content_patch_for_turn(rolling, 7) is None
    assert content_patch_pending_after(rolling, 5) is True
    assert content_patch_pending_after(rolling, 6) is False
    user, content_patch = extract_patch_input(
        "/goal 翻译后文 remain 5 今天真烦"
    )
    assert user == "今天真烦"
    assert content_patch is not None
    assert content_patch.element == "goal"
    assert content_patch.content == "翻译后文"
    assert content_patch.turns == 5

    # refresh: ends whatever is active on arrival (like roll) but the new
    # cell itself never expires on its own (like remain, turns=INF) --
    # that's what "system1 refresh, system2 refresh, ..." sandwiching needs.
    _, refreshed = extract_patch_input("/system 新提示词 refresh none")
    assert refreshed is not None
    assert refreshed.mode == ContentPatchMode.REFRESH
    assert refreshed.turns == INF
    dated = replace(refreshed, created_turn=6)
    assert content_patch_for_turn(dated, 5) is None
    assert content_patch_for_turn(dated, 6) == dated
    assert content_patch_for_turn(dated, 7) is None
    assert content_patch_pending_after(dated, 6) is False
    assert content_patch_pending_after(dated, 5) is True


if __name__ == "__main__":
    _self_test()
    print("patch.state_patch: ok")
