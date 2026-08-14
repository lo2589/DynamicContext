"""Processor protocol and the two calculation examples from the reference."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from numbers import Number
from typing import TYPE_CHECKING, Any, Optional, Union

from ..dataset.table_manager import ColumnId, RowId, SlotKey
from ..lifecycle import INF, Remain, validate_remain
from ..registry import compact

if TYPE_CHECKING:
    from ..manager.engine import Engine


class ProcessorOutputMode(str, Enum):
    INSERT = "insert"
    IN_PLACE = "in_place"


@dataclass
class ProcessorRuntime:
    last_run_turn: Optional[int] = None
    last_source_end: Optional[int] = None
    last_result_id: Optional[str] = None
    run_count: int = 0


@dataclass(frozen=True)
class InsertResult:
    processor_name: str
    column_id: str
    anchor_turn: int
    source_columns: tuple[ColumnId, ...]
    values: dict[RowId, Any]
    remain: Remain = INF


@dataclass(frozen=True)
class InPlaceResult:
    processor_name: str
    source_columns: tuple[ColumnId, ...]
    updates: dict[SlotKey, Any]


ProcessorResult = Union[InsertResult, InPlaceResult]


def is_due_periodic(
    current_turn: int,
    runtime: ProcessorRuntime,
    *,
    step: int,
    first_run_turn: int,
) -> bool:
    """Fixed-interval trigger: every ``step`` turns after ``first_run_turn``."""
    if runtime.last_run_turn is None:
        return current_turn >= first_run_turn
    return current_turn >= runtime.last_run_turn + step


def is_due_manual(requested: bool) -> bool:
    """Explicit trigger: the input channel asked for compression this turn."""
    return bool(requested)


def is_due_overload(context_size_bytes: int, *, threshold_bytes: int) -> bool:
    """Resource trigger: the assembled context has grown past a byte budget."""
    if threshold_bytes <= 0:
        raise ValueError("threshold_bytes 必须是正数")
    return context_size_bytes > threshold_bytes


def context_byte_size(context: list[dict[str, str]]) -> int:
    """Measure the input context about to be sent to the model, in bytes.

    This is the same messages list ``chat()`` receives (system/user/assistant
    roles), serialized the way it would go over the wire — UTF-8, so
    non-ASCII content (e.g. Chinese) counts multiple bytes per character,
    matching what a real payload size looks like rather than character count.
    """
    return len(json.dumps(context, ensure_ascii=False).encode("utf-8"))


def compression_window(
    current_turn: int,
    *,
    hold_back: int,
    last_result_id: Optional[str],
    last_source_end: Optional[int],
    first_window_start: Optional[int] = None,
) -> tuple[ColumnId, ...]:
    """Shared window rule for every insert-mode trigger (periodic/manual/overload).

    A run always ends at ``current_turn - hold_back`` and either starts from
    the configured first-window turn (no prior result yet) or continues from
    the previous result's own column_id plus whatever raw turns followed it.
    """
    end_turn = current_turn - hold_back
    if last_result_id is None:
        if first_window_start is None or first_window_start < 1:
            raise ValueError("首次压缩窗口起点必须 >= 1")
        if first_window_start > end_turn:
            raise ValueError("首次压缩窗口起点越过了窗口终点")
        return tuple(range(first_window_start, end_turn + 1))
    if last_source_end is None:
        raise AssertionError("有 last_result_id 就必须有 last_source_end")
    return (last_result_id, *range(last_source_end + 1, end_turn + 1))


class Processor(ABC):
    def __init__(
        self,
        *,
        name: str,
        k: int,
        m: int,
        first_run_turn: int,
        output_mode: ProcessorOutputMode,
    ):
        if not name:
            raise ValueError("processor name 不能为空")
        if k <= m or m < 0:
            raise ValueError("必须满足 k > m >= 0")
        if first_run_turn < 1:
            raise ValueError("first_run_turn 必须 >= 1")
        self.name = name
        self.k = k
        self.m = m
        self.step = k - m
        self.first_run_turn = first_run_turn
        self.output_mode = output_mode

    def is_due(self, current_turn: int, runtime: ProcessorRuntime) -> bool:
        return is_due_periodic(
            current_turn,
            runtime,
            step=self.step,
            first_run_turn=self.first_run_turn,
        )

    @abstractmethod
    def compute(self, engine: "Engine", runtime: ProcessorRuntime) -> ProcessorResult:
        raise NotImplementedError


@compact("sum")
class SumProcessor(Processor):
    """Reference insert-mode example: incrementally sum an old raw window."""

    def __init__(
        self,
        *,
        name: str = "sum",
        k: int,
        m: int,
        first_run_turn: int,
        result_remain: Remain = INF,
    ):
        super().__init__(
            name=name,
            k=k,
            m=m,
            first_run_turn=first_run_turn,
            output_mode=ProcessorOutputMode.INSERT,
        )
        validate_remain(result_remain)
        self.result_remain = result_remain

    def compute(self, engine: "Engine", runtime: ProcessorRuntime) -> InsertResult:
        current_turn = engine.current_turn
        end_turn = current_turn - self.m
        sources = compression_window(
            current_turn,
            hold_back=self.m,
            last_result_id=runtime.last_result_id,
            last_source_end=runtime.last_source_end,
            first_window_start=current_turn - self.k,
        )

        values: dict[RowId, Any] = {}
        for row_id in engine.tables.row_order:
            total: Optional[Number] = None
            for column_id in sources:
                value = engine.lifecycle.get_calculation_value(row_id, column_id)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, Number):
                    raise TypeError(
                        f"sum 只接受数值: row={row_id!r}, "
                        f"column={column_id!r}, value={value!r}"
                    )
                total = value if total is None else total + value
            values[row_id] = 0 if total is None else total

        return InsertResult(
            processor_name=self.name,
            column_id=f"{end_turn}_{self.name}",
            anchor_turn=end_turn,
            source_columns=sources,
            values=values,
            remain=self.result_remain,
        )


@compact("add_constant")
class AddConstantProcessor(Processor):
    """Reference in-place example: add one constant to displayed values."""

    def __init__(
        self,
        *,
        name: str = "add",
        k: int,
        m: int,
        first_run_turn: int,
        delta: Number = 1,
    ):
        super().__init__(
            name=name,
            k=k,
            m=m,
            first_run_turn=first_run_turn,
            output_mode=ProcessorOutputMode.IN_PLACE,
        )
        self.delta = delta

    def compute(self, engine: "Engine", runtime: ProcessorRuntime) -> InPlaceResult:
        current_turn = engine.current_turn
        start = current_turn - self.k
        end = current_turn - self.m
        sources = tuple(range(start, end + 1))
        updates: dict[SlotKey, Any] = {}
        for row_id in engine.tables.row_order:
            for column_id in sources:
                value = engine.lifecycle.get_display_value(row_id, column_id)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, Number):
                    raise TypeError("AddConstantProcessor 只接受数值")
                updates[(row_id, column_id)] = value + self.delta
        return InPlaceResult(self.name, sources, updates)


def _self_test() -> None:
    processor = SumProcessor(k=20, m=5, first_run_turn=100)
    runtime = ProcessorRuntime()
    assert processor.output_mode == ProcessorOutputMode.INSERT
    assert processor.is_due(99, runtime) is False
    assert processor.is_due(100, runtime) is True
    runtime.last_run_turn = 100
    assert processor.is_due(114, runtime) is False
    assert processor.is_due(115, runtime) is True

    periodic_runtime = ProcessorRuntime()
    assert is_due_periodic(9, periodic_runtime, step=15, first_run_turn=10) is False
    assert is_due_periodic(10, periodic_runtime, step=15, first_run_turn=10) is True
    periodic_runtime.last_run_turn = 10
    assert is_due_periodic(24, periodic_runtime, step=15, first_run_turn=10) is False
    assert is_due_periodic(25, periodic_runtime, step=15, first_run_turn=10) is True

    assert is_due_manual(False) is False
    assert is_due_manual(True) is True

    assert is_due_overload(30_000, threshold_bytes=32_000) is False
    assert is_due_overload(32_001, threshold_bytes=32_000) is True
    try:
        is_due_overload(1, threshold_bytes=0)
    except ValueError:
        pass
    else:
        raise AssertionError("threshold_bytes<=0 应该拒绝")

    ascii_context = [{"role": "user", "content": "hi"}]
    chinese_context = [{"role": "user", "content": "你好"}]
    # Same character count, but non-ASCII must count more bytes (UTF-8), not
    # be measured as if it were plain character length.
    assert context_byte_size(chinese_context) > context_byte_size(ascii_context)
    assert context_byte_size([]) == 2  # "[]"
    assert is_due_overload(
        context_byte_size(chinese_context), threshold_bytes=1
    ) is True

    first_window = compression_window(
        20, hold_back=5, last_result_id=None, last_source_end=None, first_window_start=1
    )
    assert first_window == tuple(range(1, 16))
    continued_window = compression_window(
        35, hold_back=5, last_result_id="15_summary", last_source_end=15
    )
    assert continued_window == ("15_summary", *range(16, 31))
    try:
        compression_window(20, hold_back=5, last_result_id=None, last_source_end=None)
    except ValueError:
        pass
    else:
        raise AssertionError("首次窗口没有起点应该拒绝")


if __name__ == "__main__":
    _self_test()
    print("compact.processor: ok")
