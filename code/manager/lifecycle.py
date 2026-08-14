"""Lifecycle state and transitions; no processor implementation lives here."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isinf
from typing import Any, Union

from ..dataset.table_manager import ColumnId, RowId, SlotKey, TableManager
from ..dataset.input_data import InputCell, InputValues
from ..patch.state_patch import CellPatch, RowPatch, RowPatchMode
from ..lifecycle import INF, Remain, calculate_end_turn, validate_remain
from ..registry import patch as patch_registry


NO_LIFE = None
NO_OVERRIDE = object()

PASS = "PASS"
RAW_VALUE = "RAW_VALUE"
LIFE = "LIFE"
CURRENT = "CURRENT"
RESULT_VALUE = "RESULT_VALUE"
OVERRIDE_VALUE = "OVERRIDE_VALUE"




@dataclass
class LifecycleState:
    tables: TableManager
    current_turn: int
    current_rule: dict[RowId, Remain]
    active_finite: dict[RowId, set[ColumnId]]
    active_permanent: dict[RowId, set[ColumnId]]
    expire_buckets: dict[int, set[SlotKey]]

    def clone(self) -> "LifecycleState":
        return LifecycleState(
            tables=self.tables.clone(),
            current_turn=self.current_turn,
            current_rule=dict(self.current_rule),
            active_finite={
                row_id: set(columns)
                for row_id, columns in self.active_finite.items()
            },
            active_permanent={
                row_id: set(columns)
                for row_id, columns in self.active_permanent.items()
            },
            expire_buckets={
                turn: set(cells) for turn, cells in self.expire_buckets.items()
            },
        )


class LifecycleManager:
    """Own lifecycle semantics, without owning any calculation method."""

    def __init__(self, state: LifecycleState):
        self.state = state

    @property
    def tables(self) -> TableManager:
        return self.state.tables

    @property
    def current_turn(self) -> int:
        return self.state.current_turn

    # The lifespan vocabulary itself lives in code/lifecycle; these stay as
    # methods only so existing call sites keep working.
    validate_remain = staticmethod(validate_remain)
    calculate_end_turn = staticmethod(calculate_end_turn)

    def add_row(self, row_id: RowId, remain: Remain) -> None:
        self.validate_remain(remain)
        self.tables.register_row(row_id)
        self.state.current_rule[row_id] = remain
        self.state.active_finite[row_id] = set()
        self.state.active_permanent[row_id] = set()

    def add_raw_turn(self, turn: int, values: InputValues) -> None:
        self.tables.register_column(turn, kind="raw", created_turn=turn)
        normalized = self._normalize_values(values)
        for row_id in self.tables.row_order:
            cell = self._normalize_cell(normalized.get(row_id))
            self.tables.set(PASS, row_id, turn, cell.passed, actor="input")
            if not cell.passed:
                continue
            self.tables.set(RAW_VALUE, row_id, turn, cell.value, actor="input")
            self.start_cell(
                row_id,
                turn,
                remain=self.state.current_rule[row_id],
                born_turn=turn,
            )

    def start_cell(
        self,
        row_id: RowId,
        column_id: ColumnId,
        *,
        remain: Remain,
        born_turn: int,
    ) -> None:
        self.validate_remain(remain)
        end_turn = self.calculate_end_turn(born_turn, remain)
        self.start_cell_until(
            row_id,
            column_id,
            end_turn=end_turn,
            born_turn=born_turn,
        )

    def start_cell_until(
        self,
        row_id: RowId,
        column_id: ColumnId,
        *,
        end_turn: Remain,
        born_turn: int,
        active_at: int | None = None,
    ) -> None:
        """Start a cell from an explicit exclusive end.

        Fixed history stores resolved end turns, including the zero-length
        ``[born, born)`` interval used by ephemeral slots such as ``think``.
        """
        if isinstance(end_turn, bool):
            raise ValueError("end_turn 不能是 bool")
        permanent = isinstance(end_turn, float) and isinf(end_turn)
        if not permanent and (
            not isinstance(end_turn, int) or end_turn < born_turn
        ):
            raise ValueError("end_turn 必须不早于 born_turn，或为 INF")

        is_current = active_at is None or (
            born_turn <= active_at
            and (permanent or active_at < int(end_turn))
        )
        self.tables.set(LIFE, row_id, column_id, end_turn, actor="lifecycle")
        self.tables.set(CURRENT, row_id, column_id, is_current, actor="lifecycle")

        self.state.active_finite[row_id].discard(column_id)
        self.state.active_permanent[row_id].discard(column_id)
        if not is_current:
            return

        if permanent:
            self.state.active_permanent[row_id].add(column_id)
        else:
            self.state.active_finite[row_id].add(column_id)
            self.state.expire_buckets.setdefault(int(end_turn), set()).add(
                (row_id, column_id)
            )

    def end_cell(self, row_id: RowId, column_id: ColumnId, *, turn: int) -> None:
        self.tables.set(LIFE, row_id, column_id, turn, actor="lifecycle")
        self.tables.set(CURRENT, row_id, column_id, False, actor="lifecycle")
        self.state.active_finite[row_id].discard(column_id)
        self.state.active_permanent[row_id].discard(column_id)

    def expire_turn(self, turn: int) -> None:
        for row_id, column_id in self.state.expire_buckets.pop(turn, set()):
            if not self.tables.get(CURRENT, row_id, column_id):
                continue
            if self.tables.get(LIFE, row_id, column_id) != turn:
                continue
            self.tables.set(CURRENT, row_id, column_id, False, actor="lifecycle")
            self.state.active_finite[row_id].discard(column_id)

    def apply_row_patch(self, patch: RowPatch, turn: int) -> None:
        patch_registry["row"](self, patch, turn)

    def apply_cell_patch(self, patch: CellPatch, turn: int) -> bool:
        return patch_registry["cell"](self, patch, turn)

    def add_derived_column(
        self,
        *,
        column_id: str,
        anchor_turn: int,
        producer: str,
        values: Mapping[RowId, Any],
        remain: Remain = INF,
    ) -> None:
        self.tables.register_column(
            column_id,
            kind="derived",
            created_turn=self.current_turn,
            anchor_after=anchor_turn,
            producer=producer,
        )
        for row_id in self.tables.row_order:
            if row_id not in values:
                continue
            self.tables.set(
                RESULT_VALUE,
                row_id,
                column_id,
                values[row_id],
                actor="processor",
            )
            self.start_cell(
                row_id,
                column_id,
                remain=remain,
                born_turn=self.current_turn,
            )

    def get_display_value(self, row_id: RowId, column_id: ColumnId) -> Any:
        override = self.tables.get(OVERRIDE_VALUE, row_id, column_id)
        if override is not NO_OVERRIDE:
            return override
        if not self.tables.get(CURRENT, row_id, column_id):
            return None
        meta = self.tables.column_meta[column_id]
        if meta.kind == "raw":
            if not self.tables.get(PASS, row_id, column_id):
                return None
            return self.tables.get(RAW_VALUE, row_id, column_id)
        return self.tables.get(RESULT_VALUE, row_id, column_id)

    def get_calculation_value(self, row_id: RowId, column_id: ColumnId) -> Any:
        """Read immutable raw evidence, or a derived result."""

        meta = self.tables.column_meta[column_id]
        if meta.kind == "raw":
            if not self.tables.get(PASS, row_id, column_id):
                return None
            return self.tables.get(RAW_VALUE, row_id, column_id)
        return self.tables.get(RESULT_VALUE, row_id, column_id)

    def validate(self) -> None:
        self.tables.validate()
        for row_id in self.tables.row_order:
            finite = self.state.active_finite[row_id]
            permanent = self.state.active_permanent[row_id]
            if finite & permanent:
                raise AssertionError(f"row={row_id!r} 活跃集合重叠")
            indexed = finite | permanent
            actual = {
                column_id
                for column_id in self.tables.column_order
                if self.tables.get(CURRENT, row_id, column_id)
            }
            if indexed != actual:
                raise AssertionError(
                    f"row={row_id!r} 活跃索引不一致: index={indexed}, actual={actual}"
                )

    def _normalize_values(self, values: InputValues) -> dict[RowId, Any]:
        if isinstance(values, Mapping):
            unknown = set(values) - set(self.tables.row_order)
            if unknown:
                raise ValueError(f"输入包含未知行: {sorted(unknown, key=repr)}")
            return {row_id: values.get(row_id) for row_id in self.tables.row_order}
        values_list = list(values)
        if len(values_list) > len(self.tables.row_order):
            raise ValueError("列表输入长度大于当前行数")
        values_list.extend([None] * (len(self.tables.row_order) - len(values_list)))
        return dict(zip(self.tables.row_order, values_list))

    @staticmethod
    def _normalize_cell(value: Any) -> InputCell:
        if isinstance(value, InputCell):
            return value
        if value is None:
            return InputCell(False, None)
        return InputCell(True, value)


def _self_test() -> None:
    from ..dataset.table_manager import TablePolicy

    tables = TableManager()
    tables.register_table(PASS, TablePolicy(frozenset({"input"}), lambda: False))
    tables.register_table(RAW_VALUE, TablePolicy(frozenset({"input"}), lambda: None))
    tables.register_table(LIFE, TablePolicy(frozenset({"lifecycle"}), lambda: None))
    tables.register_table(CURRENT, TablePolicy(frozenset({"lifecycle"}), lambda: False))
    tables.register_table(RESULT_VALUE, TablePolicy(frozenset({"processor"}), lambda: None))
    tables.register_table(
        OVERRIDE_VALUE,
        TablePolicy(frozenset({"processor"}), lambda: NO_OVERRIDE),
    )
    tables.register_row("goal")
    state = LifecycleState(
        tables=tables,
        current_turn=0,
        current_rule={"goal": 1},
        active_finite={"goal": set()},
        active_permanent={"goal": set()},
        expire_buckets={},
    )
    lifecycle = LifecycleManager(state)
    lifecycle.add_raw_turn(1, {"goal": "one turn"})
    assert tables.get(CURRENT, "goal", 1) is True
    lifecycle.expire_turn(2)
    assert tables.get(CURRENT, "goal", 1) is False
    lifecycle.validate()


if __name__ == "__main__":
    _self_test()
    print("manager.lifecycle: ok")
