"""Resume the one task dataset bound by a config."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Any

from .load_config import Config, load_config
from .tables import DatasetPaths, DatasetTables, load_tables
from ..registry import dataset


@dataclass(frozen=True)
class DatasetContext:
    config_path: Path
    cfg: Config
    tables: DatasetTables

    def summary(self) -> dict[str, Any]:
        return {
            "config": str(self.config_path),
            "task": self.cfg.name,
            **self.tables.summary(),
        }


@dataset("context.resume")
def resume_context(config_path: str | Path) -> DatasetContext:
    """Load existing task data; create it only when its directory is absent."""

    resolved_config = Path(config_path).expanduser().resolve()
    cfg = load_config(resolved_config)
    paths = DatasetPaths.from_config(cfg)
    tables = load_tables(paths)
    return DatasetContext(
        config_path=resolved_config,
        cfg=cfg,
        tables=tables,
    )


def _self_test() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        config_path = root / "task.yaml"
        config_path.write_text(
            "name: resume_test\n"
            "dataset:\n"
            "  type: local\n"
            "  kwargs:\n"
            "    path: task\n"
            "    tables:\n"
            "      history: history.jsonl\n"
            "      state: state_latest.json\n"
            "      context: context_latest.json\n"
            "      life_cycle: life_cycle.json\n"
            "      patches: patches.jsonl\n"
            "      compression: compression\n"
            "      raw: raw_history.jsonl\n",
            encoding="utf-8",
        )
        first = resume_context(config_path)
        second = resume_context(config_path)
        assert first.tables.created is True
        assert second.tables.created is False
        assert second.cfg.dataset.kwargs.path == "task"


if __name__ == "__main__":
    _self_test()
    print("dataset.resume_context: ok")
