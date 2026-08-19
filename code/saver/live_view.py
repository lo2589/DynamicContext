"""Reusable live-viewer HTTP server.

`viewer.html` draws the slot timetable; `serve.py` at the repo root is the CLI
that points it at a task directory by name. This module holds the reusable
half — building the page and the request handler — once, so the CLI and
`runtime.run` (which knows the task directory directly, resume included, and
doesn't need a --task name) can both start the same server without
duplicating the poller or the file-serving logic.

Reading is always on: this serves `<task_dir>/history.jsonl`, which the saver
already writes every turn. Writing is opt-in — pass ``push`` (typically
``DatasetInputProcessor.push_user``) and the page grows a real input box that
POSTs to ``/send``, which hands the text straight to that callable. That only
makes sense when a live ``RuntimeComponents`` is sitting in the same process
to consume it (the task's ``input_data.interface`` must be ``gui`` so its
turn loop is reading from that queue instead of the terminal) — the
standalone CLI, watching a run in a separate process, has nothing to hand
input to, so it never passes ``push`` and the page stays read-only, exactly
as it always has.
"""

from __future__ import annotations

import json
import re
import threading
import webbrowser
from collections.abc import Callable
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..registry import saver

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWER = REPO_ROOT / "viewer.html"

# Injected at the end of the page. The viewer defines `load(jsonl, label)` and
# calls it for every dataset button, so following a live file needs no new
# rendering path — just that function, called again when the bytes change.
POLLER = """
<script>
(function () {
  var last = null
  var label = %(label)s
  var interval = %(interval)d

  function tick() {
    return fetch('history.jsonl?t=' + Date.now(), { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.text() : null })
      .then(function (text) {
        if (text === null || text === last) return false
        last = text
        var turns = text.trim().split('\\n').length
        try { load(text, label + ' · ' + turns + ' rows · live') } catch (e) {}
        return true
      })
      .catch(function () { return false })
  }
  tick()
  setInterval(tick, interval)
  window.__liveTick = tick
})()
</script>
"""

# Appended only when a `push` callback is wired up server-side. Lives outside
# the chat/#rows DOM that load()/render() rewrite on every tick, so it is
# never at risk of being clobbered by a re-render.
SEND_BAR = """
<style>
#live-send{position:sticky;bottom:0;display:flex;gap:8px;align-items:center;
  padding:10px 16px;background:var(--panel);border-top:1px solid var(--rule);
  box-shadow:var(--shadow)}
#live-send input{flex:1;padding:8px 10px;border:1px solid var(--rule);
  border-radius:6px;background:var(--paper);color:var(--ink);
  font:inherit}
#live-send button{padding:8px 14px;border:1px solid var(--rule);
  border-radius:6px;background:var(--live);color:var(--paper);cursor:pointer;
  font:inherit}
#live-send button:disabled{opacity:.5;cursor:default}
#live-send .status{color:var(--ink-2);font-size:12px;min-width:8em}
</style>
<div id="live-send">
  <input id="live-send-text" type="text" placeholder="跟这个 run 说点什么…" autocomplete="off">
  <button id="live-send-btn">Send</button>
  <span class="status" id="live-send-status"></span>
</div>
<script>
(function () {
  var input = document.getElementById("live-send-text")
  var button = document.getElementById("live-send-btn")
  var status = document.getElementById("live-send-status")

  function send() {
    var text = input.value.trim()
    if (!text) return
    button.disabled = true
    status.textContent = "sending…"
    fetch("/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text }),
    })
      .then(function (r) { return r.ok ? r.json() : r.json().then(function (e) { throw new Error(e.error || r.statusText) }) })
      .then(function () {
        input.value = ""
        status.textContent = "waiting for reply…"
        // The turn only lands once the model finishes; poll a bit faster
        // than the configured interval right after sending so it shows up
        // as soon as it commits, without dropping the steady background poll.
        var tries = 0
        var fast = setInterval(function () {
          tries += 1
          if (tries > 40) { clearInterval(fast); status.textContent = ""; return }
          window.__liveTick().then(function (changed) {
            if (changed) { clearInterval(fast); status.textContent = "" }
          })
        }, 750)
      })
      .catch(function (err) { status.textContent = "failed: " + err.message })
      .then(function () { button.disabled = false })
  }

  button.onclick = send
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") send()
  })
})()
</script>
"""


def page(label: str, interval_ms: int, *, bare: bool = False, writable: bool = False) -> bytes:
    """The viewer, live-polling; the shipped comparison samples are dropped
    when ``bare`` (a real work session has no use for them), and a real input
    box is appended when ``writable`` (there is somewhere for it to go)."""
    html = VIEWER.read_text(encoding="utf-8")
    if bare:
        html = re.sub(r"const SAMPLES = \{.*?\};?\n", "const SAMPLES = {};\n", html, count=1)
    # Drop the auto-pick so the shipped sample does not flash before the first
    # poll lands; the poller supplies the first load.
    html = re.sub(r'pick\("[\w-]+"\)\s*;?', "", html, count=1)
    html += POLLER % {"label": json.dumps(label, ensure_ascii=False), "interval": interval_ms}
    if writable:
        html += SEND_BAR
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def __init__(
        self,
        *args,
        task_dir: Path,
        body: bytes,
        push: Callable[[str], None] | None,
        **kwargs,
    ) -> None:
        self.task_dir = task_dir
        self.body = body
        self.push = push
        super().__init__(*args, **kwargs)

    def _send(self, payload: bytes, mime: str, *, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict, *, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", status=status)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(self.body, "text/html; charset=utf-8")
        elif path == "/history.jsonl":
            history = self.task_dir / "history.jsonl"
            # A run that has not written yet is not an error: the page keeps
            # polling and picks it up when the first turn commits.
            data = history.read_bytes() if history.is_file() else b""
            self._send(data, "application/x-ndjson; charset=utf-8")
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != "/send":
            self.send_error(404)
            return
        if self.push is None:
            self._send_json(
                {"error": "此页面没有接到可写入的 runtime（用 runtime.viewer.enabled 时才会有）"},
                status=503,
            )
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload.get("text") or "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        if not text:
            self._send_json({"error": "empty text"}, status=400)
            return
        try:
            self.push(text)
        except Exception as exc:  # noqa: BLE001 - report it back to the page, don't 500 blindly
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json({"ok": True})

    def log_message(self, *_args) -> None:
        """Silence per-request logging; the poll would drown the console."""


def build_server(
    task_dir: Path,
    *,
    label: str,
    port: int,
    interval_ms: int,
    bare: bool = False,
    push: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    if not VIEWER.is_file():
        raise SystemExit(f"{VIEWER} 不存在")
    task_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(
        Handler,
        task_dir=task_dir,
        body=page(label, interval_ms, bare=bare, writable=push is not None),
        push=push,
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


@saver("viewer.serve")
def start_viewer(
    task_dir: Path,
    *,
    label: str,
    port: int = 8777,
    interval: float = 1.0,
    open_browser: bool = True,
    push: Callable[[str], None] | None = None,
    bare: bool = True,
) -> ThreadingHTTPServer:
    """Serve the live viewer for ``task_dir`` in a background thread.

    The caller decides whether a failure here (port taken, viewer.html
    missing) should be fatal — this only builds and starts the server, it
    never bounds the run's own turn loop.
    """
    server = build_server(
        task_dir, label=label, port=port, interval_ms=int(interval * 1000), bare=bare, push=push
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(
        f"[viewer] {label} → {url}  （每 {interval}s 拉一次"
        + ("，网页可直接发消息）" if push is not None else "）")
    )
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    return server


def _self_test() -> None:
    import tempfile
    import urllib.request

    # Bypass any system HTTP proxy: it has no business intercepting a
    # loopback request to a server this process just started.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with tempfile.TemporaryDirectory() as temporary:
        task_dir = Path(temporary)
        (task_dir / "history.jsonl").write_text('{"id":0}\n', encoding="utf-8")

        # Read-only mode (serve.py's use): no send bar, shipped samples kept.
        server = build_server(task_dir, label="selftest", port=0, interval_ms=500)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with opener.open(f"http://127.0.0.1:{port}/") as response:
                body = response.read()
            assert b"const SAMPLES = {\"v4" in body
            assert b"selftest" in body
            assert b"live-send" not in body
            with opener.open(f"http://127.0.0.1:{port}/history.jsonl") as response:
                data = response.read()
            assert data == b'{"id":0}\n'
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/send",
                data=b'{"text":"hi"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                opener.open(request)
                raise AssertionError("push 未接入时 /send 应该失败")
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Writable mode (runtime.viewer's use): bare page, /send reaches push.
        received: list[str] = []
        server = build_server(
            task_dir, label="live", port=0, interval_ms=500, bare=True, push=received.append
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with opener.open(f"http://127.0.0.1:{port}/") as response:
                body = response.read()
            assert b"const SAMPLES = {};" in body
            assert b"live-send" in body
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/send",
                data=json.dumps({"text": "hello from the page"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with opener.open(request) as response:
                assert json.loads(response.read()) == {"ok": True}
            assert received == ["hello from the page"]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    _self_test()
    print("saver.live_view: ok")
