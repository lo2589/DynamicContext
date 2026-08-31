#!/usr/bin/env python3
"""Open or close one named live session by its task directory.

    chat <task>     # already running: just opens the tab
                     # not running: starts task/**/<task>/runtime.yaml, then opens
    chat            # no name: reopens whichever task "open" last succeeded on
    chat close <task>
    chat close      # no name: lists what's running, closes nothing

"close" is the one reserved word — a task literally named "close" can't be
opened through the bare form, only via `python3 -c` or the panel.

Reads task/.sessions.json through the same registry every `run_runtime`
session already writes to (code/saver/session_registry.py) — a persisted
session is one already found there and answering on its port, not a
directory that merely exists. This wraps main.py; it never talks to the
runtime directly.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from code.saver.session_registry import list_sessions

REPO_ROOT = Path(__file__).resolve().parent
TASKS_DIR = REPO_ROOT / "task"
LAST_TASK_FILE = TASKS_DIR / ".last_task"
START_TIMEOUT = 20.0
POLL_INTERVAL = 0.3


def _sessions_for(task: str) -> list[dict]:
    return [s for s in list_sessions().values() if s["task"] == task]


def _find_yaml(task: str) -> Path | None:
    """Registry sessions are keyed by leaf directory name, not full path
    (runtime.py sets task=root.name) — so lookup matches the same way,
    searching task/ at any depth rather than only its immediate children."""
    if not TASKS_DIR.is_dir():
        return None
    matches = [p for p in TASKS_DIR.rglob("runtime.yaml") if p.parent.name == task]
    return matches[0] if matches else None


def _print_overview() -> None:
    running = list_sessions()
    if running:
        print("正在跑的会话：")
        for entry in sorted(running.values(), key=lambda s: s["started_at"]):
            print(f"  {entry['task']:<24} :{entry['port']}  (started {entry['started_at']})")
    else:
        print("没有正在跑的会话。")
    if TASKS_DIR.is_dir():
        openable = sorted(
            p.parent.relative_to(TASKS_DIR).as_posix()
            for p in TASKS_DIR.rglob("runtime.yaml")
        )
        if openable:
            print("有 runtime.yaml、可以直接 open 的 task（用最后一段目录名）：")
            for rel in openable:
                print(f"  {rel}")


def _remember(task: str) -> None:
    LAST_TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_TASK_FILE.write_text(task, encoding="utf-8")


def open_session(task: str) -> None:
    existing = _sessions_for(task)
    if existing:
        port = existing[0]["port"]
        print(f"{task} 已经在跑（:{port}），直接打开")
        webbrowser.open(f"http://127.0.0.1:{port}/")
        _remember(task)
        return

    yaml_path = _find_yaml(task)
    if yaml_path is None:
        print(f"没找到 task/**/{task}/runtime.yaml，没法直接启动。")
        print("先用面板建一个（python3 start.py），或者手写这份 yaml 再重试。")
        raise SystemExit(1)

    print(f"{task} 没在跑，启动 {yaml_path.relative_to(REPO_ROOT)} ...")
    subprocess.Popen(
        [sys.executable, "main.py", "--config", str(yaml_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        found = _sessions_for(task)
        if found:
            port = found[0]["port"]
            print(f"起来了（:{port}），打开浏览器")
            webbrowser.open(f"http://127.0.0.1:{port}/")
            _remember(task)
            return
        time.sleep(POLL_INTERVAL)
    print(f"{START_TIMEOUT:.0f}秒了还没在会话列表里出现——viewer 可能没开，或者启动失败，自己看一眼终端输出。")
    raise SystemExit(1)


def close_session(task: str) -> None:
    matches = _sessions_for(task)
    if not matches:
        print(f"没找到正在跑的 {task}")
        raise SystemExit(1)
    for entry in matches:
        pid = entry["pid"]
        # SIGINT, not SIGTERM: run_runtime's cleanup (session deregister,
        # the ledger's own finally) only runs on the same signal Ctrl-C
        # sends. SIGTERM's default disposition skips Python's finally
        # blocks entirely and would leave a ghost entry until the registry's
        # own self-heal on next read.
        os.kill(pid, signal.SIGINT)
        print(f"关了 {task}（:{entry['port']}, pid {pid}）")


def main() -> None:
    argv = sys.argv[1:]

    if not argv:
        # No name at all: reopen whichever task the last successful `open`
        # named, not just whatever happens to still be running — the point
        # is picking back up, even after every session has since exited.
        if LAST_TASK_FILE.is_file():
            open_session(LAST_TASK_FILE.read_text(encoding="utf-8").strip())
        else:
            _print_overview()
        return

    if argv[0] == "close":
        if len(argv) < 2:
            _print_overview()
        else:
            close_session(argv[1])
        return

    if len(argv) > 1:
        print(f"多余的参数：{' '.join(argv[1:])}（一次只开一个 task）")
        raise SystemExit(1)
    open_session(argv[0])


if __name__ == "__main__":
    main()
