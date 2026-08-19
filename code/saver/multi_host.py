"""One process hosting many conversations, on one port.

Every session used to be its own `main.py`, each binding its own viewer port
taken from its own YAML. But "which port is free" is a fact about the machine,
and a task's config cannot know what another task claimed — so two runs
configured for the same port left the second one silently invisible: bound to
nothing, listed nowhere, still consuming its input queue. The cross-process
registry, its file lock, the dead-pid healing and the hub page all existed to
paper over that split.

Hosting them in one process removes the problem rather than managing it.
Switching sessions is switching an in-memory object; port collision stops
being a concept.

Nothing here modifies the runtime. A session is built through exactly the
same entries `main.py` uses — `tables.initialize`, `input.build`,
`runtime.build`, `runtime.run` — and the per-session HTTP behaviour is the
same set of callables `live_view` is handed. The one adjustment is made on a
copy of the config in memory: `runtime.viewer.enabled` is turned off so the
session does not also try to serve itself. The YAML on disk is never
rewritten; `source_path` still points at it, so patches and settings continue
to be written back to the real file.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import live_view
from ..manager import runtime as runtime_module  # Registers tables.initialize / runtime.*.
from ..registry import dataset, manager, saver

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = REPO_ROOT / "task"
HOST_PORT = 8770


def _headless(cfg: Any) -> Any:
    """The same config with its own viewer switched off, in memory only.

    A hosted session must not open a second server for itself. Rebuilding the
    Config from a mutated dict keeps `source_path` pointing at the real YAML —
    which is what patch writes and settings writes rely on — while leaving
    that file untouched on disk.
    """

    from ..dataset.load_config import Config

    data = cfg.to_dict()
    runtime_cfg = data.setdefault("runtime", {})
    viewer = runtime_cfg.get("viewer")
    runtime_cfg["viewer"] = {**viewer, "enabled": False} if isinstance(viewer, dict) else False
    return Config.from_mapping(data, source_path=cfg.source_path)


@dataclass
class Session:
    task: str
    runtime: Any
    thread: threading.Thread | None = None

    @property
    def task_dir(self) -> Path:
        return self.runtime.tables.paths.root

    def turns(self) -> int:
        return max(0, len(self.runtime.tables.history) - 1)


@dataclass
class Host:
    sessions: dict[str, Session] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def open(self, task: str) -> Session:
        """Build a session and start its turn loop in a thread.

        Threads rather than processes: a turn spends nearly all its time
        blocked on the provider's socket, so the GIL is not the constraint,
        and sharing one address space is the entire point — the HTTP layer
        can hand a request the very RuntimeComponents the loop is using.
        """

        with self.lock:
            existing = self.sessions.get(task)
            if existing is not None:
                return existing

            yaml_path = TASKS_DIR / task / "runtime.yaml"
            if not yaml_path.is_file():
                raise ValueError(f"task/{task}/runtime.yaml 不存在")

            cfg = _headless(dataset["config.load"](yaml_path))
            tables = manager["tables.initialize"](cfg)
            input_data = dataset["input.build"](cfg, tables=tables)
            runtime = manager["runtime.build"](cfg, tables=tables, input_data=input_data)

            session = Session(task=task, runtime=runtime)
            thread = threading.Thread(
                target=manager["runtime.run"],
                args=(runtime,),
                name=f"session:{task}",
                daemon=True,
            )
            session.thread = thread
            self.sessions[task] = session
            thread.start()
            print(f"[session opened] {task}")
            return session

    def close(self, task: str) -> None:
        """Let a session's loop end. The ledger on disk is untouched."""
        with self.lock:
            session = self.sessions.pop(task, None)
        if session is None:
            return
        close = getattr(session.runtime.input_data, "close", None)
        if callable(close):
            close()
        print(f"[session closed] {task}")

    def listed(self) -> list[dict[str, Any]]:
        """Every conversation on disk, whether or not it is open here."""
        rows: list[dict[str, Any]] = []
        for directory in sorted(TASKS_DIR.iterdir()) if TASKS_DIR.is_dir() else []:
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            if not (directory / "runtime.yaml").is_file():
                continue
            session = self.sessions.get(directory.name)
            history = directory / "history.jsonl"
            turns = (
                sum(1 for line in history.read_text(encoding="utf-8").splitlines() if line.strip())
                if history.is_file()
                else 0
            )
            rows.append(
                {
                    "task": directory.name,
                    "open": session is not None,
                    "turns": max(0, turns - 1),
                }
            )
        return rows


def _bundle(session: Session) -> dict[str, Any]:
    """The per-session callables, taken from the runtime module unchanged."""

    live = session.runtime
    interface = (live.cfg.to_dict().get("input_data") or {}).get("interface")
    return {
        "push": live.input_data.push_user if interface == "gui" else None,
        "recall_preview": runtime_module._recall_preview(live),
        "compress_status": runtime_module._compress_status(live),
        "model_status": runtime_module._model_status(live),
        "model_switch": runtime_module._model_switch(live),
        "last_error": lambda: live.last_error,
        "streaming": lambda: {"text": live.streaming, "active": live.streaming_active},
        "interrupt": runtime_module._interrupt(live),
        "settings_read": runtime_module._settings_read(live),
        "settings_write": runtime_module._settings_write(live),
    }


class _TaskDirOnly:
    """Just enough of a handler for live_view's file-reading helpers.

    `_token_stats` reads nothing but `self.task_dir`, so borrowing it keeps
    one implementation of the token and prefix-cache accounting instead of a
    second copy that could drift.
    """

    def __init__(self, task_dir: Path) -> None:
        self.task_dir = task_dir


class HostHandler(BaseHTTPRequestHandler):
    host: Host

    def _send(self, payload: bytes, mime: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value: Any, *, status: int = 200) -> None:
        self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json", status=status)

    def _split(self, path: str) -> tuple[str, str] | None:
        """`/s/<task>/rest` -> (task, "/rest")."""
        if not path.startswith("/s/"):
            return None
        rest = path[3:]
        task, _, tail = rest.partition("/")
        return (task, "/" + tail) if task else None

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path.startswith("/static/"):
            live_view.serve_static(self, path)
            return
        if path in ("/", "/index.html"):
            rows = self.host.listed()
            first = next((r["task"] for r in rows if r["open"]), rows[0]["task"] if rows else None)
            if first is None:
                self._send(b"<p>task/ under this repo has no runtime.yaml yet.</p>", "text/html; charset=utf-8")
                return
            self.send_response(302)
            self.send_header("Location", f"/s/{first}/")
            self.end_headers()
            return
        if path == "/sessions":
            self._json({"sessions": self.host.listed()})
            return

        split = self._split(path)
        if split is None:
            self.send_error(404)
            return
        task, tail = split
        try:
            session = self.host.open(task)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=404)
            return

        if tail in ("/", "/index.html"):
            self._send(
                live_view.page(task, 1000, bare=True, writable=_bundle(session)["push"] is not None),
                "text/html; charset=utf-8",
            )
            return
        if tail == "/history.jsonl":
            history = session.task_dir / "history.jsonl"
            self._send(
                history.read_bytes() if history.is_file() else b"",
                "application/x-ndjson; charset=utf-8",
            )
            return
        if tail in live_view.Handler.TABLE_FILES:
            table = session.task_dir / live_view.Handler.TABLE_FILES[tail]
            mime = "application/json" if tail.endswith(".json") else "application/x-ndjson; charset=utf-8"
            default = b"{}" if tail.endswith(".json") else b""
            self._send(table.read_bytes() if table.is_file() else default, mime)
            return
        if tail == "/snapshots":
            directory = session.task_dir / "snapshots"
            names = sorted(p.name for p in directory.iterdir() if p.is_dir()) if directory.is_dir() else []
            self._json({"snapshots": names})
            return
        if tail == "/tokens":
            self._json(live_view.Handler._token_stats(_TaskDirOnly(session.task_dir)))
            return
        if tail == "/sessions":
            self._json(
                {
                    "sessions": [
                        {"task": row["task"], "port": None, "open": row["open"]}
                        for row in self.host.listed()
                    ],
                    "here": task,
                    "hosted": True,
                }
            )
            return

        bundle = _bundle(session)
        if tail == "/streaming":
            self._json(bundle["streaming"]())
            return
        if tail == "/last-error":
            self._json({"error": bundle["last_error"]()})
            return
        if tail == "/models":
            self._json(bundle["model_status"]())
            return
        if tail == "/settings":
            self._json(bundle["settings_read"]())
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/open":
            task = str(self._body().get("task") or "").strip()
            try:
                self.host.open(task)
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            self._json({"ok": True, "url": f"/s/{task}/"})
            return
        if path == "/close":
            self.host.close(str(self._body().get("task") or "").strip())
            self._json({"ok": True})
            return

        split = self._split(path)
        if split is None:
            self.send_error(404)
            return
        task, tail = split
        try:
            session = self.host.open(task)
        except ValueError as exc:
            self._json({"error": str(exc)}, status=404)
            return
        bundle = _bundle(session)

        if tail == "/interrupt":
            self._json(bundle["interrupt"]())
            return
        if tail == "/recall/preview":
            query = str(self._body().get("query") or "").strip()
            if not query:
                self._json({"error": "empty query"}, status=400)
                return
            self._json({"results": bundle["recall_preview"](query)})
            return
        if tail == "/turn":
            from . import turn_control

            try:
                self._json(turn_control.apply_action(session.runtime, self._body()))
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
            return
        if tail in ("/model", "/settings"):
            action = bundle["model_switch"] if tail == "/model" else bundle["settings_write"]
            try:
                self._json(action(self._body()))
            except (ValueError, FileNotFoundError, AssertionError) as exc:
                self._json({"error": str(exc)}, status=400)
            return
        if tail in live_view.Handler.CONTROL_PATHS:
            if bundle["push"] is None:
                self._json({"error": "这个 task 的 input_data.interface 不是 gui"}, status=503)
                return
            try:
                text = live_view.Handler._build_command(self, tail, self._body())
            except ValueError as exc:
                self._json({"error": str(exc)}, status=400)
                return
            bundle["push"](text)
            self._json({"ok": True})
            return
        self.send_error(404)

    def log_message(self, *_args) -> None:
        """Quiet: the pages poll, and the log would be nothing else."""


@saver("host.serve")
def serve_host(
    *, port: int = HOST_PORT, tasks: list[str] | None = None, open_browser: bool = True
) -> tuple[ThreadingHTTPServer, Host]:
    host = Host()
    for task in tasks or []:
        host.open(task)

    handler = type("BoundHostHandler", (HostHandler,), {"host": host})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"[host] {len(host.sessions)} 个会话 → {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server, host


def _self_test() -> None:
    import tempfile
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # _headless must switch the viewer off without touching the file.
    from ..dataset.load_config import load_config

    template = REPO_ROOT / "config" / "standard" / "runtime.yaml"
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "runtime.yaml"
        text = template.read_text(encoding="utf-8").replace(
            "    enabled: false", "    enabled: true", 1
        )
        path.write_text(text, encoding="utf-8")
        cfg = load_config(path)
        assert cfg.runtime.viewer.enabled is True
        quiet = _headless(cfg)
        assert quiet.runtime.viewer.enabled is False
        assert quiet.source_path == cfg.source_path  # patches still land in the real file
        assert "enabled: true" in path.read_text(encoding="utf-8")  # file untouched

    # Routing splits /s/<task>/rest without needing a live session.
    class _Split(HostHandler):
        def __init__(self):  # noqa: D107 - no socket, only _split is exercised
            pass

    splitter = _Split()
    assert splitter._split("/s/demo/history.jsonl") == ("demo", "/history.jsonl")
    assert splitter._split("/s/demo/") == ("demo", "/")
    assert splitter._split("/sessions") is None
    assert splitter._split("/s/") is None

    # The server answers /sessions and serves static assets on one port.
    host = Host()
    handler = type("BoundHostHandler", (HostHandler,), {"host": host})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with opener.open(f"http://127.0.0.1:{port}/sessions") as response:
            listed = json.loads(response.read())
        assert isinstance(listed["sessions"], list)
        with opener.open(f"http://127.0.0.1:{port}/static/live.js") as response:
            assert b"__liveTick" in response.read()
        try:
            opener.open(f"http://127.0.0.1:{port}/s/definitely_not_a_task/")
            raise AssertionError("不存在的 task 应该 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    _self_test()
    print("saver.multi_host: ok")
