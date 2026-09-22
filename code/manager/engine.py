"""Transaction boundary joining tables, lifecycle, patches, and processors."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isinf
from typing import Any, Union

from ..compact.processor import (
    AddConstantProcessor,
    InPlaceResult,
    InsertResult,
    Processor,
    ProcessorResult,
    ProcessorRuntime,
    SumProcessor,
)
from ..dataset.table_manager import ColumnId, RowId, TableManager, TablePolicy
from ..dataset.input_data import StreamItem, TurnInput
from ..patch.state_patch import CellPatch, Patch, RowPatch
from ..registry import manager
from .lifecycle import (
    CURRENT,
    INF,
    LIFE,
    NO_LIFE,
    NO_OVERRIDE,
    OVERRIDE_VALUE,
    PASS,
    RAW_VALUE,
    RESULT_VALUE,
    LifecycleManager,
    LifecycleState,
    Remain,
)
from ..lifecycle import resolve_bound


@dataclass
class EngineState:
    lifecycle_state: LifecycleState
    processor_runtime: dict[str, ProcessorRuntime]

    def clone(self) -> "EngineState":
        return EngineState(
            lifecycle_state=self.lifecycle_state.clone(),
            processor_runtime={
                name: ProcessorRuntime(
                    last_run_turn=runtime.last_run_turn,
                    last_source_end=runtime.last_source_end,
                    last_result_id=runtime.last_result_id,
                    run_count=runtime.run_count,
                )
                for name, runtime in self.processor_runtime.items()
            },
        )


class Engine:
    def __init__(
        self,
        initial_remains: Union[Sequence[Remain], Mapping[RowId, Remain]],
    ):
        tables = TableManager()
        tables.register_table(
            PASS,
            TablePolicy(frozenset({"input"}), lambda: False, append_only=True),
        )
        tables.register_table(
            RAW_VALUE,
            TablePolicy(frozenset({"input"}), lambda: None, append_only=True),
        )
        tables.register_table(
            LIFE,
            TablePolicy(frozenset({"lifecycle"}), lambda: NO_LIFE),
        )
        tables.register_table(
            CURRENT,
            TablePolicy(frozenset({"lifecycle"}), lambda: False),
        )
        tables.register_table(
            RESULT_VALUE,
            TablePolicy(
                frozenset({"processor"}),
                lambda: None,
                append_only=True,
            ),
        )
        tables.register_table(
            OVERRIDE_VALUE,
            TablePolicy(
                frozenset({"processor"}),
                lambda: NO_OVERRIDE,
                allow_delete=True,
            ),
        )

        items = (
            list(initial_remains.items())
            if isinstance(initial_remains, Mapping)
            else list(enumerate(initial_remains))
        )
        current_rule: dict[RowId, Remain] = {}
        active_finite: dict[RowId, set[ColumnId]] = {}
        active_permanent: dict[RowId, set[ColumnId]] = {}
        for row_id, remain in items:
            LifecycleManager.validate_remain(remain)
            tables.register_row(row_id)
            current_rule[row_id] = remain
            active_finite[row_id] = set()
            active_permanent[row_id] = set()

        self._state = EngineState(
            lifecycle_state=LifecycleState(
                tables=tables,
                current_turn=0,
                current_rule=current_rule,
                active_finite=active_finite,
                active_permanent=active_permanent,
                expire_buckets={},
            ),
            processor_runtime={},
        )
        self.processors: dict[str, Processor] = {}
        self.committed_turns = 0
        self.rolled_back_turns = 0

    @property
    def lifecycle(self) -> LifecycleManager:
        return LifecycleManager(self._state.lifecycle_state)

    @property
    def tables(self) -> TableManager:
        return self._state.lifecycle_state.tables

    @property
    def current_turn(self) -> int:
        return self._state.lifecycle_state.current_turn

    def register_processor(self, processor: Processor) -> None:
        if processor.name in self.processors:
            raise ValueError(f"processor={processor.name!r} 已注册")
        self.processors[processor.name] = processor
        self._state.processor_runtime[processor.name] = ProcessorRuntime()

    def take_snapshot(self) -> EngineState:
        return self._state.clone()

    def restore_snapshot(self, snapshot: EngineState) -> None:
        candidate = snapshot.clone()
        LifecycleManager(candidate.lifecycle_state).validate()
        self._state = candidate

    def process_turn(self, item: StreamItem) -> int:
        working = self._state.clone()
        working_engine = self._from_state(working, self.processors)
        try:
            working_engine._process_turn_in_place(item)
            working_engine.validate()
        except Exception:
            self.rolled_back_turns += 1
            raise
        self._state = working_engine._state
        self.committed_turns += 1
        return self.current_turn

    @classmethod
    def _from_state(
        cls,
        state: EngineState,
        processors: dict[str, Processor],
    ) -> "Engine":
        engine = cls.__new__(cls)
        engine._state = state
        engine.processors = processors
        engine.committed_turns = 0
        engine.rolled_back_turns = 0
        return engine

    def _process_turn_in_place(self, item: StreamItem) -> None:
        turn_input = item if isinstance(item, TurnInput) else TurnInput(item)
        turn = self.current_turn + 1
        lifecycle = self.lifecycle

        pre_patches: list[Patch] = []
        current_patches: list[CellPatch] = []
        for patch in turn_input.patches:
            if isinstance(patch, CellPatch) and patch.column_id == turn:
                current_patches.append(patch)
            else:
                pre_patches.append(patch)

        for patch in pre_patches:
            if isinstance(patch, RowPatch):
                lifecycle.apply_row_patch(patch, turn)
            else:
                lifecycle.apply_cell_patch(patch, turn)

        lifecycle.expire_turn(turn)
        lifecycle.add_raw_turn(turn, turn_input.values)
        lifecycle.state.current_turn = turn
        for patch in current_patches:
            lifecycle.apply_cell_patch(patch, turn)
        self._run_due_processors()

    def _run_due_processors(self) -> None:
        for name, processor in self.processors.items():
            runtime = self._state.processor_runtime[name]
            if not processor.is_due(self.current_turn, runtime):
                continue
            result = processor.compute(self, runtime)
            self._apply_processor_result(result)
            runtime.last_run_turn = self.current_turn
            runtime.last_source_end = self.current_turn - processor.m
            if isinstance(result, InsertResult):
                runtime.last_result_id = result.column_id
            runtime.run_count += 1

    def _apply_processor_result(self, result: ProcessorResult) -> None:
        if isinstance(result, InsertResult):
            self.lifecycle.add_derived_column(
                column_id=result.column_id,
                anchor_turn=result.anchor_turn,
                producer=result.processor_name,
                values=result.values,
                remain=result.remain,
            )
            self._retire_source_columns(result)
            return
        if isinstance(result, InPlaceResult):
            for (row_id, column_id), value in result.updates.items():
                self.tables.set(
                    OVERRIDE_VALUE,
                    row_id,
                    column_id,
                    value,
                    actor="processor",
                    allow_overwrite=True,
                )
            return
        raise TypeError(f"未知 ProcessorResult={type(result).__name__}")

    def _retire_source_columns(self, result: InsertResult) -> None:
        """Close every source cell a computation just consumed.

        The result now stands in for them; a source column may itself be an
        earlier result's column_id, so retiring cascades naturally.
        """
        current_turn = self.current_turn
        for row_id in result.values:
            for column_id in result.source_columns:
                if column_id not in self.tables.column_meta:
                    continue
                if self.tables.get(CURRENT, row_id, column_id):
                    self.lifecycle.end_cell(row_id, column_id, turn=current_turn)

    def validate(self) -> None:
        self.lifecycle.validate()
        for name, runtime in self._state.processor_runtime.items():
            if (
                runtime.last_result_id is not None
                and runtime.last_result_id not in self.tables.column_meta
            ):
                raise AssertionError(f"processor={name!r} 结果列丢失")

    def run(self, stream: Sequence[StreamItem]) -> None:
        for item in stream:
            self.process_turn(item)


FixedHistory = dict[str, dict[str, dict[str, Any]]]
FixedState = dict[str, dict[str, int]]


class FixedTableLifecycle:
    """Project fixed history cells onto the existing lifecycle engine."""

    def __init__(
        self,
        history: FixedHistory,
        life_cycle: Mapping[str, Any],
        *,
        current_turn: int,
    ) -> None:
        self.history = history
        self.life_cycle = dict(life_cycle)
        self.engine = Engine({row_id: INF for row_id in self._row_order()})
        self.engine.lifecycle.state.current_turn = current_turn

        for turn_id in sorted(self.history, key=int):
            turn = int(turn_id)
            self.engine.tables.register_column(turn, kind="raw", created_turn=turn)
            for element, slot in self.history[turn_id].items():
                self.engine.tables.set(PASS, element, turn, True, actor="input")
                self.engine.tables.set(
                    RAW_VALUE,
                    element,
                    turn,
                    str(slot.get("content") or ""),
                    actor="input",
                )
                self._project_cell(element, turn, slot, current_turn)
        self.validate()

    def _project_cell(self, element: str, born: int, slot: Mapping[str, Any], at: int) -> None:
        self.engine.lifecycle.start_cell_until(
            element, born, end_turn=self._internal_end(slot), born_turn=born, active_at=at,
        )
        # A cell may start after its anchor or have gaps between reopened ranges.
        visible = any(
            int(entry[0][0]) <= at and (entry[0][1] is None or at <= int(entry[0][1]))
            for entry in slot.get("range", [])
        ) and (slot.get("dead") is None or at < int(slot["dead"]))
        if not visible:
            self.engine.tables.set(CURRENT, element, born, False, actor="lifecycle")
            self.engine.lifecycle.state.active_finite[element].discard(born)
            self.engine.lifecycle.state.active_permanent[element].discard(born)

    @property
    def current_turn(self) -> int:
        return self.engine.current_turn

    def expire_turn(self, turn: int) -> None:
        if turn < self.current_turn:
            raise ValueError("不能向过去推进生命周期")
        self.engine.lifecycle.expire_turn(turn)
        self.engine.lifecycle.state.current_turn = turn
        for born, row in self.history.items():
            for element, slot in row.items():
                self._project_cell(element, int(born), slot, turn)

    def add_slot(
        self,
        element: str,
        content: str,
        *,
        turn: int,
        remain: Remain | None = None,
        visible_from: int | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        self._ensure_row(element)
        self._ensure_column(turn)
        turn_key = str(turn)
        if element in self.history.setdefault(turn_key, {}):
            raise ValueError(
                f"history[{turn}][{element}] 已存在，不能覆盖同一生命周期单元格"
            )

        if remain is None:
            ranges = self._configured_range(element, turn, visible_from)
            end_turn = self._internal_end({"range": ranges})
        else:
            LifecycleManager.validate_remain(remain)
            start = turn if visible_from is None else visible_from
            end_turn = LifecycleManager.calculate_end_turn(start, remain)
            ranges = [[[start, self._external_end(end_turn)], 1.0, "none"]]

        self.engine.tables.set(PASS, element, turn, True, actor="input")
        self.engine.tables.set(
            RAW_VALUE, element, turn, str(content), actor="input"
        )
        born = turn if visible_from is None else visible_from
        self.engine.lifecycle.start_cell_until(
            element,
            turn,
            end_turn=end_turn,
            born_turn=born,
            active_at=born,
        )
        slot: dict[str, Any] = {"content": str(content), "range": ranges}
        if extra:
            slot.update(dict(extra))
        self.history[turn_key][element] = slot

    def end_active(self, element: str, *, turn: int) -> None:
        if element not in self.engine.tables.row_order:
            return
        active = (
            self.engine.lifecycle.state.active_finite[element]
            | self.engine.lifecycle.state.active_permanent[element]
        )
        for column_id in list(active):
            if not isinstance(column_id, int) or column_id >= turn:
                continue
            self.engine.lifecycle.end_cell(element, column_id, turn=turn)
            slot = self.history[str(column_id)][element]
            for entry in slot.get("range") or []:
                scope = self._scope(entry)
                end = scope[1]
                if end is None or int(end) >= turn:
                    scope[1] = turn - 1

    def end_cell(self, element: str, born_turn: int, *, turn: int) -> None:
        """Close exactly one (element, born_turn) cell, not the whole row.

        ``end_active`` closes every currently active occurrence of an element
        name; that is wrong for collapsing a single scoped range (e.g. the
        user/assistant turns a goal governed) because elements like ``user``
        never expire on their own and are active at every prior turn.
        """
        if element not in self.engine.tables.row_order:
            return
        active = (
            self.engine.lifecycle.state.active_finite[element]
            | self.engine.lifecycle.state.active_permanent[element]
        )
        if born_turn not in active:
            return
        self.engine.lifecycle.end_cell(element, born_turn, turn=turn)
        slot = self.history[str(born_turn)][element]
        for entry in slot.get("range") or []:
            scope = self._scope(entry)
            end = scope[1]
            if end is None or int(end) >= turn:
                scope[1] = turn - 1

    def reopen_cell(self, element: str, born_turn: int, *, turn: int) -> None:
        """Make an already-written, currently invisible cell visible again by
        appending a fresh range segment starting at ``turn``.

        This is why ``range`` is a list rather than one interval: a slot can
        be visible over several disjoint windows without its content ever
        being recomputed. A summary written once at its own compaction turn
        can therefore be pulled back out of history later — no second pass
        over the model, no new slot, just another segment of visibility.
        """
        if element not in self.engine.tables.row_order:
            raise ValueError(f"{element} 不存在，无法重新捞起")
        slot = self.history.get(str(born_turn), {}).get(element)
        if slot is None:
            raise ValueError(f"history[{born_turn}][{element}] 不存在，无法重新捞起")
        active = (
            self.engine.lifecycle.state.active_finite[element]
            | self.engine.lifecycle.state.active_permanent[element]
        )
        if born_turn in active:
            return  # already visible; nothing to append
        # Coming back does not hand a slot a longer life than it declared. A
        # declaration with a static end still ends there; only one that never
        # states an end (permanent, or a dynamic rule awaiting its event) gets
        # an open segment, closed later by whatever closes it.
        end = self._declared_end(element, born_turn)
        if end is not None and end < turn:
            raise ValueError(
                f"life_cycle.{element} 声明的有效期已在第 {end} 轮结束，无法在第 {turn} 轮捞起"
            )
        density, compressor = self._segment_style(slot)
        self.engine.lifecycle.start_cell_until(
            element,
            born_turn,
            end_turn=INF if end is None else end + 1,
            born_turn=born_turn,
            active_at=turn,
        )
        slot.setdefault("range", []).append([[turn, end], density, compressor])

    def state_snapshot(self) -> FixedState:
        return {
            turn_id: {
                element: int(
                    self.engine.tables.get(CURRENT, element, int(turn_id))
                )
                for element in content
            }
            for turn_id, content in self.history.items()
        }

    def validate(self) -> None:
        self.engine.validate()
        for turn_id, content in self.history.items():
            turn = int(turn_id)
            for element, slot in content.items():
                if self.engine.tables.get(LIFE, element, turn) != self._internal_end(slot):
                    raise AssertionError(
                        f"history[{turn_id}][{element}] range 与 LIFE 不一致"
                    )

    def _row_order(self) -> list[str]:
        rows = list(self.life_cycle)
        for turn_id in sorted(self.history, key=int):
            for element in self.history[turn_id]:
                if element not in rows:
                    rows.append(element)
        return rows

    def _ensure_row(self, element: str) -> None:
        if element not in self.engine.tables.row_order:
            self.engine.lifecycle.add_row(element, INF)

    def _ensure_column(self, turn: int) -> None:
        if turn not in self.engine.tables.column_meta:
            self.engine.tables.register_column(turn, kind="raw", created_turn=turn)

    def _declared_end(self, element: str, born_turn: int) -> int | None:
        """The closed-interval end life_cycle declares for this element, or
        None when the declaration names no end of its own."""

        raw_rule = self.life_cycle.get(element, ["born", None])
        if not isinstance(raw_rule, (list, tuple)) or len(raw_rule) != 2:
            raise ValueError(f"life_cycle.{element} 必须是 [start, end]")
        try:
            return resolve_bound(raw_rule[1], born_turn)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"life_cycle.{element} 的结束规则无效：{exc}") from exc

    @classmethod
    def _segment_style(cls, slot: Mapping[str, Any]) -> tuple[Any, Any]:
        """Carry the density and compressor of the segment a slot last had,
        so reappearing never silently re-renders it differently."""

        ranges = slot.get("range")
        if not isinstance(ranges, list) or not ranges:
            return 1.0, "none"
        last = ranges[-1]
        cls._scope(last)
        return last[1], last[2]

    def _configured_range(
        self, element: str, turn: int, visible_from: int | None = None
    ) -> list[list[Any]]:
        """Where a slot sits and when it starts speaking are two questions.

        For almost every slot they coincide: it is written at the turn it
        belongs to. A summary is the exception — it belongs to the turn it
        covers up to, but it does not exist until the compaction that
        produced it runs, several turns later. ``visible_from`` separates
        the two so the ledger never claims a slot was visible before it was
        written."""

        raw_rule = self.life_cycle.get(element, ["born", None])
        if not isinstance(raw_rule, (list, tuple)) or len(raw_rule) != 2:
            raise ValueError(f"life_cycle.{element} 必须是 [start, end]")
        raw_start, raw_end = raw_rule
        try:
            start = resolve_bound(raw_start, turn)
            end = resolve_bound(raw_end, turn)
        except (KeyError, ValueError) as exc:
            raise ValueError(f"life_cycle.{element}: {exc}") from exc
        if start is None:
            raise ValueError(f"life_cycle.{element} 的起始轮不能为空")
        if visible_from is not None:
            if visible_from < turn:
                raise ValueError(
                    f"life_cycle.{element} 的起效轮不能早于落位轮"
                )
            start = visible_from
        if end is not None and end < start:
            raise ValueError(f"life_cycle.{element} 的结束轮早于起效轮")
        # A closed interval: end is the last turn still shown (inclusive), so
        # [born, born] means "visible for exactly the turn it's born in" —
        # the same reading for every element. Both bounds go through the same
        # named-rule resolver, so "born" means the same number wherever it is
        # written, and a dynamic rule (until_*) simply resolves to None here
        # and gets its real end written back later by whatever detects the
        # event. Nothing branches on element name; think configured like
        # assistant behaves identically.
        return [[[start, end], 1.0, "none"]]

    @classmethod
    def _internal_end(cls, slot: Mapping[str, Any]) -> Remain:
        """Convert the persisted, closed-interval range end (last turn still
        shown, inclusive) into the exclusive end_turn the internal Engine
        expects (first turn no longer shown)."""

        ranges = slot.get("range")
        if not isinstance(ranges, list) or not ranges:
            raise ValueError("history slot.range 必须是非空列表")
        ends = [cls._scope(entry)[1] for entry in ranges]
        if any(end is None for end in ends):
            return INF
        return max(int(end) for end in ends) + 1

    @staticmethod
    def _external_end(end_turn: Remain) -> int | None:
        """Convert the internal Engine's exclusive end_turn back into the
        persisted, closed-interval range end (inclusive)."""

        if isinstance(end_turn, float) and isinf(end_turn):
            return None
        return int(end_turn) - 1

    @staticmethod
    def _scope(entry: Any) -> list[Any]:
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or not isinstance(entry[0], list)
            or len(entry[0]) != 2
        ):
            raise ValueError("range entry 必须是 [[start, end], density, compressor]")
        return entry[0]


@manager("lifecycle.fixed")
def build_fixed_table_lifecycle(
    history: FixedHistory,
    life_cycle: Mapping[str, Any],
    *,
    current_turn: int,
) -> FixedTableLifecycle:
    return FixedTableLifecycle(history, life_cycle, current_turn=current_turn)


def _self_test() -> None:
    from ..patch.state_patch import CellPatch, RowPatch, RowPatchMode

    engine = Engine({"a": 3, "b": INF})
    engine.process_turn({"a": 10, "b": 100})
    try:
        engine.process_turn(
            TurnInput(
                values={"a": 11, "b": 101},
                patches=(
                    RowPatch("a", RowPatchMode.FORWARD, 1),
                    CellPatch("missing", 1, 2),
                ),
            )
        )
    except KeyError:
        pass
    else:
        raise AssertionError("事务回滚检查未生效")
    assert engine.current_turn == 1
    assert engine.tables.get(RAW_VALUE, "a", 1) == 10

    sum_engine = Engine({"x": 1})
    sum_engine.register_processor(SumProcessor(k=20, m=5, first_run_turn=100))
    for _ in range(100):
        sum_engine.process_turn({"x": 1})
    assert sum_engine.tables.get(RESULT_VALUE, "x", "95_sum") == 16
    for _ in range(15):
        sum_engine.process_turn({"x": 1})
    assert sum_engine.tables.get(RESULT_VALUE, "x", "110_sum") == 31

    # A row that never expires on its own (remain=INF, like user/assistant)
    # must still have its consumed source columns retired by the processor.
    retire_engine = Engine({"y": INF})
    retire_engine.register_processor(
        SumProcessor(name="retire_sum", k=5, m=1, first_run_turn=10)
    )
    for _ in range(9):
        retire_engine.process_turn({"y": 1})
    for turn in range(5, 10):
        assert retire_engine.tables.get(CURRENT, "y", turn) is True
    retire_engine.process_turn({"y": 1})  # turn 10: sources = range(5, 10)
    for turn in range(5, 10):
        assert retire_engine.tables.get(CURRENT, "y", turn) is False
    assert retire_engine.tables.get(CURRENT, "y", 10) is True
    assert retire_engine.tables.get(RESULT_VALUE, "y", "9_retire_sum") == 5

    # The next run's source_columns includes the previous result's column_id;
    # retiring it too means a superseded summary itself stops being current.
    # step = k - m = 4, so the next run is due at turn 14 (10 + 4).
    retire_engine.process_turn({"y": 1})  # turn 11, not due yet
    retire_engine.process_turn({"y": 1})  # turn 12, not due yet
    retire_engine.process_turn({"y": 1})  # turn 13, not due yet
    assert retire_engine.tables.get(CURRENT, "y", "9_retire_sum") is True
    retire_engine.process_turn({"y": 1})  # turn 14: sources = ("9_retire_sum", 10..13)
    assert retire_engine.tables.get(CURRENT, "y", "9_retire_sum") is False
    assert retire_engine.tables.get(CURRENT, "y", 10) is False
    assert retire_engine.tables.get(CURRENT, "y", "13_retire_sum") is True
    assert retire_engine.tables.get(CURRENT, "y", 14) is True

    in_place = Engine({"x": INF})
    in_place.register_processor(
        AddConstantProcessor(k=5, m=1, first_run_turn=10, delta=1)
    )
    for value in range(1, 11):
        in_place.process_turn({"x": value})
    assert in_place.tables.get(RAW_VALUE, "x", 5) == 5
    assert in_place.lifecycle.get_display_value("x", 5) == 6
    assert all(not isinstance(column, str) for column in in_place.tables.column_order)

    patched = Engine({"x": 3, "y": INF})
    patched.run(
        [
            {"x": 1, "y": 10},
            {"x": 2, "y": 20},
            TurnInput(
                values={"x": 3, "y": 30, "z": 100},
                patches=(
                    RowPatch("z", RowPatchMode.ADD_ROW, 2),
                    RowPatch("x", RowPatchMode.FORWARD, 1),
                    CellPatch("y", 1, 2),
                ),
            ),
        ]
    )
    assert patched.tables.row_order == ["x", "y", "z"]
    assert patched.tables.get(CURRENT, "y", 1) is False
    assert patched.tables.get(RAW_VALUE, "z", 3) == 100


def run_demo() -> None:
    """Run the numeric example shipped in the reference file."""

    engine = Engine({"r1": INF, "r2": INF, "r3": INF, "r4": INF, "r5": INF})
    engine.register_processor(SumProcessor(k=20, m=5, first_run_turn=100))
    values = {"r1": 1, "r2": 2, "r3": 3, "r4": 4, "r5": 5}
    for _ in range(115):
        engine.process_turn(values)
    print("current_turn =", engine.current_turn)
    print(
        "95_sum =",
        {
            row: engine.tables.get(RESULT_VALUE, row, "95_sum")
            for row in engine.tables.row_order
        },
    )
    print(
        "110_sum =",
        {
            row: engine.tables.get(RESULT_VALUE, row, "110_sum")
            for row in engine.tables.row_order
        },
    )
    print("95 附近 =", engine.tables.column_order[92:99])
    index = engine.tables.column_order.index(110)
    print("110 附近 =", engine.tables.column_order[index - 2 : index + 5])


if __name__ == "__main__":
    _self_test()
    print("manager.engine: ok")
    run_demo()
