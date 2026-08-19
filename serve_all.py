"""Host every conversation in one process, on one port.

    python3 serve_all.py                  # open every task that has a runtime.yaml
    python3 serve_all.py --task a --task b
    python3 serve_all.py --port 9000 --no-open

Each conversation is still an ordinary YAML-driven run: the same
`tables.initialize` / `input.build` / `runtime.build` / `runtime.run` entries
`main.py` uses, against the same unmodified `task/<name>/runtime.yaml`. The
difference is that they share a process and a port, so switching between them
is switching an in-memory object and two runs can no longer collide over a
port number.

`main.py` is untouched and still the way to run a single task by itself,
which is what makes a run reproducible on its own.
"""

from __future__ import annotations

import argparse

from code.saver.multi_host import HOST_PORT, TASKS_DIR, serve_host


def _all_tasks() -> list[str]:
    if not TASKS_DIR.is_dir():
        return []
    return sorted(
        directory.name
        for directory in TASKS_DIR.iterdir()
        if directory.is_dir()
        and not directory.name.startswith(".")
        and (directory / "runtime.yaml").is_file()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", action="append", default=None, help="只开这些 task，可重复")
    parser.add_argument("--port", type=int, default=HOST_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    tasks = args.task if args.task else _all_tasks()
    if not tasks:
        raise SystemExit("task/ 下没有带 runtime.yaml 的任务；先用 python3 start.py 建一个")

    server, host = serve_host(port=args.port, tasks=tasks, open_browser=not args.no_open)
    for task in host.sessions:
        print(f"  {task}  →  http://127.0.0.1:{args.port}/s/{task}/")
    print("Ctrl-C 停止全部")
    try:
        while True:
            server._BaseServer__is_shut_down.wait(1)  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        print("\n停了")


if __name__ == "__main__":
    main()
