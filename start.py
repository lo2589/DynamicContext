"""Open the start panel: pick a model and a shape of run, then launch it.

    python3 start.py              # panel on :8775
    python3 start.py --port 9000
    python3 start.py --no-open

This is the way in when there is no config yet. It writes
`task/<name>/runtime.yaml` (and the provider config it references) from your
answers, then runs `main.py --config` against that file — so what starts is a
perfectly ordinary YAML-driven session you can re-run, edit, or hand to
someone else without the panel ever being involved again.

Already have a YAML? Skip this entirely:

    python3 main.py --config task/<name>/runtime.yaml
"""

from __future__ import annotations

import argparse

from code.saver.launcher import LAUNCHER_PORT, start_launcher


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=LAUNCHER_PORT)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server = start_launcher(port=args.port, open_browser=not args.no_open)
    print("Ctrl-C 关掉面板（已经启动的会话不受影响，它们是独立进程）")
    try:
        while True:
            server._BaseServer__is_shut_down.wait(1)  # type: ignore[attr-defined]
    except KeyboardInterrupt:
        print("\n面板关了")


if __name__ == "__main__":
    main()
