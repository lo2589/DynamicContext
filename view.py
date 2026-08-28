"""Open a run in the slot timetable.

The viewer in `viewer.html` already draws everything: the
conversation on the left, one row per slot kind, a column per turn, and a cell
that lights while its interval covers the turn you are standing on. What it does
not do is find your run — it ships recorded samples and a file picker.

This writes a copy of that page with one more sample in it: yours, selected on
open. Nothing about the runtime or the viewer changes; the page is a copy, and
the original keeps its own.

    python3 view.py                      # task/standard
    python3 view.py --task dialogue      # task/dialogue
    python3 view.py --out /tmp/run.html  # somewhere else
    python3 view.py --no-open            # just write it

The page is self-contained: no server, no file picker, and it survives being
sent to someone else.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 根目录这份是新的（对话在左、格子在右、滑块拖轮次）；
# doc/ 下那两份是 7 月的旧版，留着不动。
VIEWER = HERE / "viewer.html"

# The viewer builds one button per sample from `SAMPLES` itself and opens on
# `Object.keys(SAMPLES)[0]`, so a run only has to be first in that object — no
# button to clone, no default to rewrite.
SAMPLES_DECL = "const SAMPLES = "
RUN_KEY = "this_run"


def load_run(task_dir: Path) -> tuple[str, dict]:
    history = task_dir / "history.jsonl"
    if not history.is_file():
        raise SystemExit(f"{history} 不存在——这个 task 还没跑过？")
    text = history.read_text(encoding="utf-8")

    life_cycle = {}
    declared = task_dir / "life_cycle.json"
    if declared.is_file():
        try:
            life_cycle = json.loads(declared.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            life_cycle = {}
    return text, life_cycle


def describe(text: str, life_cycle: dict, task: str) -> str:
    """The button's label: what this run is, in the same voice as the shipped four."""
    turns = 0
    slots: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        turns = max(turns, int(row.get("id", 0)))
        slots.update((row.get("content") or {}).keys())
    declared = ", ".join(f"{k}{v}" for k, v in list(life_cycle.items())[:3]) if life_cycle else ""
    tail = f" · {declared}" if declared else ""
    return f"{task} · {turns} turns · {len(slots)} slot types{tail}"


def build(page: str, jsonl: str, label: str) -> str:
    start = page.index(SAMPLES_DECL) + len(SAMPLES_DECL)
    end = page.index("\n", start)
    samples = json.loads(page[start:end].rstrip(";"))
    # First key wins the page's opening pick; the recorded samples stay after it
    # for comparison.
    merged = {RUN_KEY: {"label": label, "jsonl": jsonl}, **samples}
    page = page[:start] + json.dumps(merged, ensure_ascii=False) + page[end:]
    return page + markdown_layer()


def markdown_layer() -> str:
    """The same Markdown renderer the served page gets, inlined.

    A model answers with a table; viewer.html only escapes it, so every
    newline collapses and the table arrives as one unreadable line. The live
    server links these two files from /static — an exported page has no
    server, and it is meant to survive being sent to someone else, so they go
    in the file itself. Same sources, no second copy of the code.
    """

    web = HERE / "web"
    css = (web / "markdown.css").read_text(encoding="utf-8")
    js = (web / "markdown.js").read_text(encoding="utf-8")
    return f"\n<style>{css}</style>\n<script>\n{js}\n</script>\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", default="standard", help="task/ 下面的目录名")
    parser.add_argument("--out", type=Path, help="输出的 html，默认写在 task 目录里")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    task_dir = HERE / "task" / args.task
    if not task_dir.is_dir():
        available = sorted(p.name for p in (HERE / "task").iterdir() if p.is_dir())
        raise SystemExit(f"没有 task/{args.task}；现有的：{', '.join(available) or '（空）'}")
    if not VIEWER.is_file():
        raise SystemExit(f"{VIEWER} 不存在")

    jsonl, life_cycle = load_run(task_dir)
    label = describe(jsonl, life_cycle, args.task)
    out = args.out or (task_dir / "view.html")
    out.write_text(build(VIEWER.read_text(encoding="utf-8"), jsonl, label), encoding="utf-8")

    print(f"{label}\n→ {out}")
    if not args.no_open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.run([opener, str(out)], check=False)


if __name__ == "__main__":
    main()
