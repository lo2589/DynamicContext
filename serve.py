"""Serve the slot timetable live, following a run as it happens.

`view.py` freezes a run into one page. This does the other thing: it serves the
same viewer with a poller attached, so a turn committed by the runtime shows
up as a new column without anyone reloading anything.

    python3 serve.py                     # task/standard on :8777
    python3 serve.py --task dialogue --port 9000
    python3 serve.py --interval 0.5      # poll faster

Nothing about the runtime changes. This reads `task/<name>/history.jsonl` off
disk, which the saver already writes each turn. The server itself lives in
`code/saver/live_view.py` — `runtime.run` starts the same one in-process when
`runtime.viewer.enabled` is set in the task YAML, so a run watched from a
second terminal (this script) and a run that opens its own tab go through
identical code.
"""

from __future__ import annotations

import argparse
import threading
import webbrowser
from pathlib import Path

from code.saver.live_view import VIEWER, build_server

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", default="standard")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--interval", type=float, default=1.0, help="轮询间隔（秒）")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    task_dir = HERE / "task" / args.task
    if not VIEWER.is_file():
        raise SystemExit(f"{VIEWER} 不存在")

    server = build_server(
        task_dir,
        label=args.task,
        port=args.port,
        interval_ms=int(args.interval * 1000),
    )
    url = f"http://127.0.0.1:{args.port}/"

    print(f"task/{args.task}  →  {url}   （每 {args.interval}s 拉一次，Ctrl-C 停）")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停了")


if __name__ == "__main__":
    main()
