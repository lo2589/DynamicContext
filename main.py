"""Visible assembly order: YAML -> four tables/resume -> input -> runtime loop."""

from code.dataset.load_config import load_cfg
from code.manager import manager
from code.registry import dataset
import code.manager.runtime  # Register table/runtime/context manager entries.


if __name__ == "__main__":
    cfg = load_cfg()

    # Keep the fixed tables and resume boundary visible at the entry point.
    tables = manager["tables.initialize"](cfg)
    input_data = dataset["input.build"](cfg, tables=tables)

    runtime = manager["runtime.build"](
        cfg,
        tables=tables,
        input_data=input_data,
    )
    manager["runtime.run"](runtime)
