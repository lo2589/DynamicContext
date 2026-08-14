"""Task-bound persistent data tables.

One config points to one dataset directory. History stores one JSON object per
turn; patches remain an append log; state and context are latest snapshots;
compression is generated data.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .history import History, load_history, save_byline, save_history
from .load_config import Config, ConfigFieldError
from ..registry import dataset, saver


DEFAULT_TABLE_NAMES = {
    "history": "history.jsonl",
    "state": "state_latest.json",
    "context": "context_latest.json",
    "life_cycle": "life_cycle.json",
    "patches": "patches.jsonl",
    "compression": "compression",
    "raw": "raw_history.jsonl",
}

DEFAULT_STATE: dict[str, Any] = {}
DEFAULT_CONTEXT: list[dict[str, str]] = []
DEFAULT_LIFE_CYCLE: dict[str, Any] = {}


@dataclass(frozen=True)
class DatasetPaths:
    root: Path
    history: Path
    state: Path
    context: Path
    life_cycle: Path
    patches: Path
    compression: Path
    raw: Path

    @classmethod
    def from_config(cls, cfg: Config) -> "DatasetPaths":
        return dataset[f"storage.{cfg.dataset.type}"](cfg)

    def as_dict(self) -> dict[str, str]:
        return {
            "root": str(self.root),
            "history": str(self.history),
            "state": str(self.state),
            "context": str(self.context),
            "life_cycle": str(self.life_cycle),
            "patches": str(self.patches),
            "compression": str(self.compression),
        }


@dataset("storage.local")
def build_local_paths(cfg: Config) -> DatasetPaths:
    raw_path = cfg.dataset.kwargs.path
    if not isinstance(raw_path, str):
        raise ConfigFieldError(
            cfg.source_path,
            "dataset.kwargs.path",
            f"应为 str，实际为 {type(raw_path).__name__}",
        )
    if not raw_path.strip():
        raise ConfigFieldError(
            cfg.source_path,
            "dataset.kwargs.path",
            "不能为空",
        )

    configured_root = Path(raw_path).expanduser()
    root = (
        configured_root.resolve()
        if configured_root.is_absolute()
        else (cfg.source_path.parent / configured_root).resolve()
    )
    names = {
        "history": cfg.dataset.kwargs.tables.history,
        "state": cfg.dataset.kwargs.tables.state,
        "context": cfg.dataset.kwargs.tables.context,
        "life_cycle": cfg.dataset.kwargs.tables.life_cycle,
        "patches": cfg.dataset.kwargs.tables.patches,
        "compression": cfg.dataset.kwargs.tables.compression,
        "raw": cfg.dataset.kwargs.tables.raw,
    }
    return DatasetPaths(
        root=root,
        history=_table_path(root, names["history"], cfg, "history"),
        state=_table_path(root, names["state"], cfg, "state"),
        context=_table_path(root, names["context"], cfg, "context"),
        life_cycle=_table_path(root, names["life_cycle"], cfg, "life_cycle"),
        patches=_table_path(root, names["patches"], cfg, "patches"),
        compression=_table_path(root, names["compression"], cfg, "compression"),
        raw=_table_path(root, names["raw"], cfg, "raw"),
    )


@dataclass
class DatasetTables:
    paths: DatasetPaths
    history: History
    state: dict[str, Any]
    context: list[dict[str, str]]
    life_cycle: dict[str, Any]
    patches: list[dict[str, Any]]
    created: bool = False

    @saver("history.save")
    def save_history(
        self,
        history: History,
    ) -> None:
        save_history(self.paths.history, history)
        self.history = load_history(self.paths.history)

    @saver("history.save_byline")
    def save_byline(
        self,
        turn_id: int,
        content: dict[str, dict[str, Any]],
    ) -> None:
        save_byline(self.paths.history, turn_id, content)
        self.history[str(turn_id)] = content

    @saver("state.save")
    def save_state(self, state: dict[str, Any]) -> None:
        _write_json_atomic(self.paths.state, state)
        self.state = dict(state)

    @saver("context.save")
    def save_context(self, context: list[dict[str, str]]) -> None:
        _write_json_atomic(self.paths.context, context)
        self.context = list(context)

    @saver("life_cycle.save")
    def save_life_cycle(self, life_cycle: dict[str, Any]) -> None:
        _write_json_atomic(self.paths.life_cycle, life_cycle)
        self.life_cycle = dict(life_cycle)

    @saver("patch.append")
    def append_patch(self, patch: dict[str, Any]) -> None:
        _append_jsonl(self.paths.patches, patch)
        self.patches.append(dict(patch))

    @saver("raw.append")
    def append_raw(self, turn_id: int, text: str) -> None:
        """Keep what the provider actually returned, before anything was split off.

        The ledger records elements, and elements are what a parser decided the
        answer contained. That decision cannot be checked, revised, or re-run against
        a different parser once the text it read is gone. This file is not a table the
        runtime reads back — it is the only copy of the input to parsing.
        """

        _append_jsonl(self.paths.raw, {"id": turn_id, "raw": text})

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.paths.root),
            "created": self.created,
            "history_records": len(self.history),
            "patch_records": len(self.patches),
            "compression_files": sum(1 for path in self.paths.compression.rglob("*") if path.is_file()),
            "tables": self.paths.as_dict(),
        }


@dataset("tables.load")
def load_tables(paths: DatasetPaths) -> DatasetTables:
    """Resume existing tables, or initialize a new task directory."""

    created = not paths.root.exists()
    _ensure_tables(paths)
    return DatasetTables(
        paths=paths,
        history=load_history(paths.history),
        state=_read_state_snapshot(paths.state),
        context=_read_json_list(paths.context, DEFAULT_CONTEXT),
        life_cycle=_read_json(paths.life_cycle, DEFAULT_LIFE_CYCLE),
        patches=_read_jsonl(paths.patches),
        created=created,
    )


def _ensure_tables(paths: DatasetPaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.history.touch(exist_ok=True)
    if not paths.state.exists():
        _write_json_atomic(paths.state, DEFAULT_STATE)
    if not paths.context.exists():
        _write_json_atomic(paths.context, DEFAULT_CONTEXT)
    if not paths.life_cycle.exists():
        _write_json_atomic(paths.life_cycle, DEFAULT_LIFE_CYCLE)


def _table_path(root: Path, value: Any, config: Config, key: str) -> Path:
    field_path = f"dataset.kwargs.tables.{key}"
    if not isinstance(value, str) or not value.strip():
        raise ConfigFieldError(config.source_path, field_path, "必须是非空字符串")
    path = (root / value).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ConfigFieldError(
            config.source_path,
            field_path,
            "必须位于 dataset.kwargs.path 内",
        ) from exc
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL: {path}:{line_number}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row must be an object: {path}:{line_number}")
        rows.append(value)
    return rows


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return dict(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON table must be an object: {path}")
    return value


def _read_json_list(
    path: Path,
    default: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return list(default)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {path}") from exc
    # Migrate the previous snapshot wrapper without retaining its duplicate turn.
    if isinstance(value, dict) and isinstance(value.get("context"), list):
        value = value["context"]
    if not isinstance(value, list):
        raise ValueError(f"JSON table must be a list: {path}")
    return value


def _read_state_snapshot(path: Path) -> dict[str, Any]:
    value = _read_json(path, DEFAULT_STATE)
    # Previous versions stored {"turn": N, "state": {...}}.
    if isinstance(value.get("state"), dict):
        return value["state"]
    return value


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _self_test() -> None:
    from .load_config import Config

    with tempfile.TemporaryDirectory() as temporary:
        config = Config.from_mapping(
            {
                "dataset": {
                    "type": "local",
                    "kwargs": {"path": "task", "tables": DEFAULT_TABLE_NAMES},
                }
            },
            source_path=Path(temporary) / "config.yaml",
        )
        paths = DatasetPaths.from_config(config)
        tables = load_tables(paths)
        assert tables.created is True
        tables.save_byline(
            0,
            {
                "system": {
                    "content": "system",
                    "range": [[[0, None], 1.0, "none"]],
                }
            },
        )
        tables.save_byline(
            1,
            {
                "user": {
                    "content": "hello",
                    "range": [[[1, None], 1.0, "none"]],
                }
            },
        )
        full_history = dict(tables.history)
        full_history["1"]["user"]["range"][0][0][1] = 3
        tables.save_history(full_history)
        tables.save_state({"1": {"user": 1}})
        tables.save_context([{"role": "user", "content": "hello"}])
        tables.save_life_cycle({"user": ["born", None]})
        tables.append_patch({"turn": 1, "type": "goal"})

        resumed = load_tables(paths)
        assert resumed.created is False
        assert resumed.history["1"]["user"]["content"] == "hello"
        assert resumed.history["1"]["user"]["range"][0][0][1] == 3
        assert resumed.state == {"1": {"user": 1}}
        assert resumed.context == [{"role": "user", "content": "hello"}]
        assert resumed.life_cycle == {"user": ["born", None]}
        assert resumed.patches[0]["type"] == "goal"

        _write_json_atomic(paths.state, {"turn": 1, "state": {"1": {"user": 1}}})
        _write_json_atomic(
            paths.context,
            {"turn": 1, "context": [{"role": "user", "content": "hello"}]},
        )
        migrated = load_tables(paths)
        assert migrated.state == {"1": {"user": 1}}
        assert migrated.context == [{"role": "user", "content": "hello"}]


if __name__ == "__main__":
    _self_test()
    print("dataset.tables: ok")
