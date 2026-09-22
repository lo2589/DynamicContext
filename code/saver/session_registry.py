"""Multi-session registry + a tiny cross-session hub page.

Each `run_runtime` session registers itself in one shared file
(`task/.sessions.json`) — chosen over a registry outside the repo because
this project has no other global/daemon state today: every other registry
here (`code/registry.py`'s eight `FolderRegistry`s) is in-process and
decorator-based. This is the first one that has to survive across processes,
so it gets exactly one file, living where every other task-private thing
already lives (`task/*` is gitignored except `task/standard/`), not a new
persistence paradigm outside the repo.

Multiple processes each read, mutate, and atomically replace the whole file,
serialized by a short-lived POSIX `flock` on a sibling `.lock` file — two
sessions starting within the same instant is not a corner case here (every
`run_runtime` registers itself within its first second alive), so unlike
`history.jsonl`/`patches.jsonl` elsewhere in this codebase, a lost concurrent
write is not an acceptable outcome for the one file multiple processes share.
A stale entry (process killed before it reached its `finally`) self-heals:
`session.list` drops every entry that cannot be serving and rewrites the file
when it does. Three things disqualify one — a pid that is gone, a pid that
exists only as an unreaped corpse, and a port a later session provably took
— because a listing this is wrong about does not merely show a dead run, it
hands somebody a link into a different conversation.

The hub is one more tiny HTTP server, on a fixed well-known port, so a user
juggling several sessions has exactly one URL to remember instead of one per
task. Whichever `run_runtime` binds that port first serves it; every other
session's bind attempt fails silently and it just runs its own viewer — the
hub is a convenience, never a dependency (nothing else reads or waits on it).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import subprocess
import tempfile
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

from ..registry import saver

REPO_ROOT = Path(__file__).resolve().parents[2]
SESSIONS_FILE = REPO_ROOT / "task" / ".sessions.json"
HUB_PORT = 8776
# Where "start a new run" lives, so a session page can offer it without
# importing the launcher (which would pull the whole panel into every run).
LAUNCHER_HINT = 8775


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize read-modify-write across processes with a POSIX flock on a
    sibling lock file — held only for the few lines that read, mutate, and
    replace the registry, never across an HTTP request."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / f".{path.name}.lock"
    with open(lock_path, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_registry(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_registry(path: Path, data: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # exists, just owned by someone else
    except OSError:
        return False  # ProcessLookupError, or an out-of-range/invalid pid
    return True


def _zombie_pids(pids: set[int]) -> set[int]:
    """Which of these pids are a process in name only.

    `os.kill(pid, 0)` answers "does this pid exist", and that stays true for a
    process that has exited and has not been reaped — which is exactly what a
    session killed with SIGKILL leaves behind while its parent is still
    running. So the entry never self-heals, and by the time the port it used
    has been handed to the next session, picking that dead task from the
    switcher opens somebody else's conversation instead. That is not a stale
    listing; that is being sent to the wrong session.

    `ps` is the portable way to ask for the state and not merely the
    existence, in one call for all of them. If it cannot be run we have
    learned nothing, so nothing is dropped.
    """

    if not pids:
        return set()
    try:
        listing = subprocess.run(
            ["ps", "-o", "pid=,state=", "-p", ",".join(str(pid) for pid in sorted(pids))],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    zombies = set()
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].startswith("Z"):
            try:
                zombies.add(int(fields[0]))
            except ValueError:
                continue
    return zombies


def _one_per_port(registry: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Drop every claim on a port that someone else is provably holding.

    A port can be bound once: `build_server` either gets it or raises, and a
    session that raises never reaches `session.register`. So two entries
    naming the same port cannot both be serving, whatever their pids say —
    and the one that got the bind is the one that started later.
    """

    newest: dict[int, str] = {}
    for session_id, entry in registry.items():
        port = int(entry.get("port", -1))
        held = newest.get(port)
        if held is None or str(entry.get("started_at") or "") >= str(
            registry[held].get("started_at") or ""
        ):
            newest[port] = session_id
    kept = set(newest.values())
    return {sid: entry for sid, entry in registry.items() if sid in kept}


@saver("session.register")
def register_session(
    *,
    task: str,
    task_dir: Path,
    port: int,
    label: str,
    registry_path: Path = SESSIONS_FILE,
) -> str:
    """IF-17: called once when a session's viewer comes up."""
    session_id = f"{task}:{port}"
    with _locked(registry_path):
        registry = _read_registry(registry_path)
        registry[session_id] = {
            "task": task,
            "task_dir": str(task_dir),
            "port": port,
            "label": label,
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        _write_registry(registry_path, registry)
    return session_id


@saver("session.deregister")
def deregister_session(session_id: str, *, registry_path: Path = SESSIONS_FILE) -> None:
    """IF-17: called from run_runtime's finally, on any exit path."""
    with _locked(registry_path):
        registry = _read_registry(registry_path)
        if registry.pop(session_id, None) is not None:
            _write_registry(registry_path, registry)


@saver("session.list")
def list_sessions(*, registry_path: Path = SESSIONS_FILE) -> dict[str, dict[str, Any]]:
    """IF-18 (as a plain function; also served over HTTP by the hub below)."""
    with _locked(registry_path):
        registry = _read_registry(registry_path)
        pids = {int(entry.get("pid", -1)) for entry in registry.values()}
        unreaped = _zombie_pids({pid for pid in pids if pid > 0})
        live = {
            session_id: entry
            for session_id, entry in registry.items()
            if _pid_alive(int(entry.get("pid", -1)))
            and int(entry.get("pid", -1)) not in unreaped
        }
        live = _one_per_port(live)
        if live != registry:
            _write_registry(registry_path, live)
    return live


HUB_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Live sessions</title>
<style>
:root{--bg:#EDF1F2;--fg:#101A1E;--fg-2:#546A70;--rule:rgba(16,26,30,.14);--live:#0D7D6C}
@media (prefers-color-scheme: dark){:root{--bg:#0C1214;--fg:#DAE5E7;--fg-2:#88A0A7;--rule:rgba(218,229,231,.16);--live:#3FBFA8}}
body{font:14px/1.6 ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;margin:2rem;background:var(--bg);color:var(--fg)}
h1{font-size:15px;color:var(--fg-2);text-transform:uppercase;letter-spacing:.04em;margin:0 0 1rem}
table{border-collapse:collapse;width:100%;max-width:48rem}
td,th{padding:6px 14px;text-align:left;border-bottom:1px solid var(--rule)}
th{color:var(--fg-2);font-weight:normal}
a{color:var(--live);text-decoration:none}
a:hover{text-decoration:underline}
#empty{color:var(--fg-2);display:none}
</style></head>
<body>
<h1>Live sessions</h1>
<table><thead><tr><th>task</th><th>label</th><th>port</th><th>started</th></tr></thead>
<tbody id="rows"></tbody></table>
<p id="empty">No live sessions.</p>
<script>
function tick(){
  fetch('/sessions', {cache:'no-store'}).then(function(r){return r.json()}).then(function(data){
    var rows = document.getElementById('rows')
    var keys = Object.keys(data)
    document.getElementById('empty').style.display = keys.length ? 'none' : 'block'
    rows.innerHTML = keys.map(function(k){
      var s = data[k]
      return '<tr><td>' + s.task + '</td><td>' + s.label + '</td>' +
        '<td><a href="http://127.0.0.1:' + s.port + '/" target="_blank">' + s.port + '</a></td>' +
        '<td>' + s.started_at + '</td></tr>'
    }).join('')
  }).catch(function(){})
}
tick()
setInterval(tick, 2000)
</script>
</body></html>
"""


class HubHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, registry_path: Path, **kwargs) -> None:
        self.registry_path = registry_path
        super().__init__(*args, **kwargs)

    def _send(self, payload: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(HUB_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/sessions":
            data = list_sessions(registry_path=self.registry_path)
            self._send(json.dumps(data).encode("utf-8"), "application/json")
        else:
            self.send_error(404)

    def log_message(self, *_args) -> None:
        """Silence per-request logging; the 2s poll would drown the console."""


@saver("hub.serve")
def start_hub(
    *, port: int = HUB_PORT, registry_path: Path = SESSIONS_FILE
) -> ThreadingHTTPServer | None:
    """IF-19: bind the well-known hub port, or return None if someone else
    already has it. Every session calls this; at most one wins, and losing
    is not an error — see module docstring."""
    handler = partial(HubHandler, registry_path=registry_path)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _self_test() -> None:
    import tempfile
    import urllib.error
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with tempfile.TemporaryDirectory() as temporary:
        registry_path = Path(temporary) / "sessions.json"

        session_id = register_session(
            task="demo", task_dir=Path(temporary), port=9001, label="demo · live",
            registry_path=registry_path,
        )
        assert session_id == "demo:9001"
        live = list_sessions(registry_path=registry_path)
        assert set(live) == {"demo:9001"}
        assert live["demo:9001"]["pid"] == os.getpid()

        # A dead pid (never allocated / long gone) self-heals out of the file.
        dead = _read_registry(registry_path)
        dead["ghost:9002"] = {
            "task": "ghost", "task_dir": "x", "port": 9002, "label": "ghost",
            "pid": 999999999, "started_at": "1970-01-01T00:00:00",
        }
        _write_registry(registry_path, dead)
        assert set(_read_registry(registry_path)) == {"demo:9001", "ghost:9002"}
        live = list_sessions(registry_path=registry_path)
        assert set(live) == {"demo:9001"}  # ghost dropped
        assert set(_read_registry(registry_path)) == {"demo:9001"}  # and rewritten to disk

        # An unreaped child is a pid that still exists and is still nothing.
        # os.kill(pid, 0) says it is there; only its state says otherwise.
        corpse = os.fork()
        if corpse == 0:  # pragma: no cover - the child only has to stop being
            os._exit(0)
        for _ in range(200):
            if corpse in _zombie_pids({corpse}):
                break
            time.sleep(0.01)
        assert _pid_alive(corpse), "the point of the case: the pid is still there"
        assert _zombie_pids({corpse}) == {corpse}
        haunted = _read_registry(registry_path)
        haunted["corpse:9003"] = {
            "task": "corpse", "task_dir": "x", "port": 9003, "label": "corpse",
            "pid": corpse, "started_at": "1970-01-01T00:00:00",
        }
        _write_registry(registry_path, haunted)
        assert set(list_sessions(registry_path=registry_path)) == {"demo:9001"}
        os.waitpid(corpse, 0)

        # Two entries, one port: only the later start can hold the bind, and
        # this is the case that used to send a switch to the wrong session.
        shared = _read_registry(registry_path)
        shared["squatter:9001"] = {
            "task": "squatter", "task_dir": "x", "port": 9001, "label": "squatter",
            "pid": os.getpid(), "started_at": "1970-01-01T00:00:00",
        }
        _write_registry(registry_path, shared)
        assert set(list_sessions(registry_path=registry_path)) == {"demo:9001"}
        assert set(_read_registry(registry_path)) == {"demo:9001"}

        deregister_session(session_id, registry_path=registry_path)
        assert list_sessions(registry_path=registry_path) == {}

        # Hub: serves the page and the same list over HTTP.
        register_session(
            task="demo", task_dir=Path(temporary), port=9001, label="demo · live",
            registry_path=registry_path,
        )
        server = start_hub(port=0, registry_path=registry_path)
        assert server is not None
        try:
            port = server.server_address[1]
            with opener.open(f"http://127.0.0.1:{port}/") as response:
                assert b"Live sessions" in response.read()
            with opener.open(f"http://127.0.0.1:{port}/sessions") as response:
                data = json.loads(response.read())
            assert set(data) == {"demo:9001"}
            assert data["demo:9001"]["port"] == 9001

            # A second hub trying the same port loses gracefully, not an error.
            second = start_hub(port=port, registry_path=registry_path)
            assert second is None
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    _self_test()
    print("saver.session_registry: ok")
