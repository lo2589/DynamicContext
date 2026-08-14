"""In-memory two-dimensional tables from the lifecycle engine reference."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Callable, Hashable, Optional, Union


RowId = Hashable
ColumnId = Union[int, str]
SlotKey = tuple[RowId, ColumnId]


@dataclass(frozen=True)
class TablePolicy:
    """Write and delete policy for one registered table."""

    owners: frozenset[str]
    default_factory: Callable[[], Any]
    append_only: bool = False
    allow_delete: bool = False


@dataclass(frozen=True)
class ColumnMeta:
    column_id: ColumnId
    kind: str
    created_turn: int
    anchor_turn: Optional[int] = None
    producer: Optional[str] = None


class TableManager:
    """Register and manage tables whose rows and columns have stable IDs."""

    def __init__(self) -> None:
        self._policies: dict[str, TablePolicy] = {}
        self._tables: dict[str, dict[RowId, dict[ColumnId, Any]]] = {}
        self._written: dict[str, set[SlotKey]] = {}
        self.row_order: list[RowId] = []
        self.column_order: list[ColumnId] = []
        self.column_meta: dict[ColumnId, ColumnMeta] = {}

    def clone(self) -> "TableManager":
        cloned = TableManager()
        cloned._policies = dict(self._policies)
        cloned._written = {
            table_id: set(cells) for table_id, cells in self._written.items()
        }
        cloned.row_order = list(self.row_order)
        cloned.column_order = list(self.column_order)
        cloned.column_meta = dict(self.column_meta)
        cloned._tables = {
            table_id: {row_id: dict(row) for row_id, row in table.items()}
            for table_id, table in self._tables.items()
        }
        return cloned

    def register_table(self, table_id: str, policy: TablePolicy) -> None:
        if table_id in self._tables:
            raise ValueError(f"table_id={table_id!r} 已注册")
        self._policies[table_id] = policy
        self._written[table_id] = set()
        self._tables[table_id] = {
            row_id: {
                column_id: policy.default_factory()
                for column_id in self.column_order
            }
            for row_id in self.row_order
        }

    def register_row(self, row_id: RowId, *, actor: str = "admin") -> None:
        self._require_admin(actor)
        if row_id in self.row_order:
            raise ValueError(f"row_id={row_id!r} 已存在")
        self.row_order.append(row_id)
        for table_id, table in self._tables.items():
            policy = self._policies[table_id]
            table[row_id] = {
                column_id: policy.default_factory()
                for column_id in self.column_order
            }

    def register_column(
        self,
        column_id: ColumnId,
        *,
        kind: str,
        created_turn: int,
        anchor_after: Optional[ColumnId] = None,
        producer: Optional[str] = None,
        actor: str = "admin",
    ) -> None:
        self._require_admin(actor)
        if column_id in self.column_meta:
            raise ValueError(f"column_id={column_id!r} 已存在")

        if anchor_after is None:
            insert_index = len(self.column_order)
            anchor_turn = None
        else:
            if anchor_after not in self.column_meta:
                raise KeyError(f"anchor column={anchor_after!r} 不存在")
            insert_index = self.column_order.index(anchor_after) + 1
            while insert_index < len(self.column_order):
                next_id = self.column_order[insert_index]
                if self.column_meta[next_id].anchor_turn != anchor_after:
                    break
                insert_index += 1
            anchor_turn = int(anchor_after) if isinstance(anchor_after, int) else None

        self.column_order.insert(insert_index, column_id)
        self.column_meta[column_id] = ColumnMeta(
            column_id=column_id,
            kind=kind,
            created_turn=created_turn,
            anchor_turn=anchor_turn,
            producer=producer,
        )
        for table_id, table in self._tables.items():
            policy = self._policies[table_id]
            for row_id in self.row_order:
                table[row_id][column_id] = policy.default_factory()

    def get(self, table_id: str, row_id: RowId, column_id: ColumnId) -> Any:
        self._require_cell(table_id, row_id, column_id)
        return self._tables[table_id][row_id][column_id]

    def set(
        self,
        table_id: str,
        row_id: RowId,
        column_id: ColumnId,
        value: Any,
        *,
        actor: str,
        allow_overwrite: bool = False,
    ) -> None:
        self._require_cell(table_id, row_id, column_id)
        policy = self._policies[table_id]
        self._require_owner(policy, actor, table_id)
        cell_key = (row_id, column_id)
        if (
            policy.append_only
            and cell_key in self._written[table_id]
            and not allow_overwrite
        ):
            raise PermissionError(
                f"table={table_id!r} 是 append-only，"
                f"cell=({row_id!r},{column_id!r}) 已有值"
            )
        self._tables[table_id][row_id][column_id] = value
        self._written[table_id].add(cell_key)

    def delete_cell(
        self,
        table_id: str,
        row_id: RowId,
        column_id: ColumnId,
        *,
        actor: str,
    ) -> None:
        self._require_cell(table_id, row_id, column_id)
        policy = self._policies[table_id]
        self._require_owner(policy, actor, table_id)
        if not policy.allow_delete:
            raise PermissionError(f"table={table_id!r} 不允许物理删除")
        self._tables[table_id][row_id][column_id] = policy.default_factory()
        self._written[table_id].discard((row_id, column_id))

    def delete_row(self, row_id: RowId, *, actor: str = "admin") -> None:
        self._require_admin(actor)
        if row_id not in self.row_order:
            raise KeyError(f"row_id={row_id!r} 不存在")
        self.row_order.remove(row_id)
        for table_id, table in self._tables.items():
            del table[row_id]
            self._written[table_id] = {
                cell for cell in self._written[table_id] if cell[0] != row_id
            }

    def delete_column(self, column_id: ColumnId, *, actor: str = "admin") -> None:
        self._require_admin(actor)
        if column_id not in self.column_meta:
            raise KeyError(f"column_id={column_id!r} 不存在")
        self.column_order.remove(column_id)
        del self.column_meta[column_id]
        for table_id, table in self._tables.items():
            for row in table.values():
                del row[column_id]
            self._written[table_id] = {
                cell for cell in self._written[table_id] if cell[1] != column_id
            }

    def read_row(
        self,
        table_id: str,
        row_id: RowId,
        *,
        columns: Optional[Sequence[ColumnId]] = None,
    ) -> list[Any]:
        self._require_table(table_id)
        self._require_row(row_id)
        selected = self.column_order if columns is None else list(columns)
        return [self.get(table_id, row_id, column_id) for column_id in selected]

    def read_range(
        self,
        table_id: str,
        row_id: RowId,
        start_column: ColumnId,
        end_column: ColumnId,
    ) -> list[tuple[ColumnId, Any]]:
        self._require_table(table_id)
        self._require_row(row_id)
        if start_column not in self.column_meta or end_column not in self.column_meta:
            raise KeyError("range 的起止列不存在")
        start_index = self.column_order.index(start_column)
        end_index = self.column_order.index(end_column)
        if start_index > end_index:
            raise ValueError("start_column 必须位于 end_column 之前")
        return [
            (column_id, self.get(table_id, row_id, column_id))
            for column_id in self.column_order[start_index : end_index + 1]
        ]

    def table(self, table_id: str) -> dict[RowId, dict[ColumnId, Any]]:
        self._require_table(table_id)
        return self._tables[table_id]

    def validate(self) -> None:
        row_set = set(self.row_order)
        column_set = set(self.column_order)
        if len(row_set) != len(self.row_order):
            raise AssertionError("row_order 有重复 ID")
        if len(column_set) != len(self.column_order):
            raise AssertionError("column_order 有重复 ID")
        if column_set != set(self.column_meta):
            raise AssertionError("column_order 与 column_meta 不一致")

        valid_cells = {
            (row_id, column_id)
            for row_id in row_set
            for column_id in column_set
        }
        for table_id, table in self._tables.items():
            if not self._written[table_id] <= valid_cells:
                raise AssertionError(f"table={table_id!r} written 索引越界")
            if set(table) != row_set:
                raise AssertionError(f"table={table_id!r} 行集合不一致")
            for row_id, row in table.items():
                if set(row) != column_set:
                    raise AssertionError(
                        f"table={table_id!r}, row={row_id!r} 列集合不一致"
                    )

    def _require_table(self, table_id: str) -> None:
        if table_id not in self._tables:
            raise KeyError(f"table_id={table_id!r} 未注册")

    def _require_row(self, row_id: RowId) -> None:
        if row_id not in self.row_order:
            raise KeyError(f"row_id={row_id!r} 不存在")

    def _require_column(self, column_id: ColumnId) -> None:
        if column_id not in self.column_meta:
            raise KeyError(f"column_id={column_id!r} 不存在")

    def _require_cell(self, table_id: str, row_id: RowId, column_id: ColumnId) -> None:
        self._require_table(table_id)
        self._require_row(row_id)
        self._require_column(column_id)

    @staticmethod
    def _require_admin(actor: str) -> None:
        if actor != "admin":
            raise PermissionError("结构增删只允许 actor='admin'")

    @staticmethod
    def _require_owner(policy: TablePolicy, actor: str, table_id: str) -> None:
        if actor not in policy.owners and actor != "admin":
            raise PermissionError(
                f"actor={actor!r} 无权写 table={table_id!r}; "
                f"owners={sorted(policy.owners)}"
            )


def _self_test() -> None:
    tables = TableManager()
    tables.register_table(
        "T",
        TablePolicy(frozenset({"writer"}), lambda: 0, allow_delete=True),
    )
    tables.register_row("r")
    tables.register_column(1, kind="raw", created_turn=1)
    tables.set("T", "r", 1, 7, actor="writer")
    assert tables.read_range("T", "r", 1, 1) == [(1, 7)]
    tables.delete_cell("T", "r", 1, actor="writer")
    assert tables.get("T", "r", 1) == 0

    tables.register_table(
        "APPEND",
        TablePolicy(frozenset({"writer"}), lambda: None, append_only=True),
    )
    tables.set("APPEND", "r", 1, None, actor="writer")
    try:
        tables.set("APPEND", "r", 1, "second", actor="writer")
    except PermissionError:
        pass
    else:
        raise AssertionError("append-only 写入检查未生效")
    tables.validate()


if __name__ == "__main__":
    _self_test()
    print("table_manager: ok")
