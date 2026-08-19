"""Run every module's _self_test once, in one interpreter.

Running a submodule with ``python -m`` re-executes it after its package
``__init__`` already imported it, which trips the registries' duplicate-name
guard. Importing each module once and calling its ``_self_test`` directly
avoids that, and keeps the whole suite to a single process.
"""

from __future__ import annotations

import importlib
import sys
import traceback


MODULES = (
    "code.registry",
    "code.dataset.table_manager",
    "code.dataset.load_config",
    "code.dataset.history",
    "code.dataset.tables",
    "code.dataset.resume_context",
    "code.dataset.input_data",
    "code.patch.state_patch",
    "code.provider.answer_parser",
    "code.provider.provider",
    "code.compact.processor",
    "code.compact.summery",
    "code.compact.collapse",
    "code.lifecycle.rules",
    "code.recall.grep_recall",
    "code.recall.trigger",
    "code.manager.lifecycle",
    "code.manager.context",
    "code.manager.engine",
    "code.manager.runtime",
    "code.saver.table_printer",
    "code.saver.live_view",
    "code.saver.session_registry",
    "code.saver.launcher",
    "code.saver.multi_host",
    "code.saver.turn_control",
)


def main() -> int:
    failures: list[str] = []
    skipped: list[str] = []
    for name in MODULES:
        try:
            module = importlib.import_module(name)
        except Exception:
            failures.append(name)
            print(f"IMPORT FAIL  {name}")
            traceback.print_exc()
            continue
        self_test = getattr(module, "_self_test", None)
        if self_test is None:
            skipped.append(name)
            print(f"no _self_test {name}")
            continue
        try:
            self_test()
        except Exception:
            failures.append(name)
            print(f"FAIL         {name}")
            traceback.print_exc()
        else:
            print(f"ok           {name}")

    print()
    print(f"{len(MODULES) - len(failures) - len(skipped)} passed, "
          f"{len(failures)} failed, {len(skipped)} without tests")
    if failures:
        print("failed:", ", ".join(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
