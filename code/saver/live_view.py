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

from ..dataset.input_data import MANUAL_COMPRESS_COMMAND
from ..registry import saver

REPO_ROOT = Path(__file__).resolve().parents[2]
VIEWER = REPO_ROOT / "viewer.html"

# Last context seen per task, and the prefix-reuse figure derived from it.
# Providers cache a prompt by its leading messages, so what matters is how
# much of this turn's context is byte-identical to last turn's *from the
# front* — one edit to an early message invalidates everything after it. That
# is exactly what this runtime does when a life_cycle retires an old slot or
# a compaction replaces a stretch of turns, so the number puts a price on
# those decisions. Kept in memory: it is a property of the transition between
# two turns, not of any file, and a fresh process has not seen a transition.
_CACHE_STATE: dict[str, dict] = {}

# The front end lives in web/ as ordinary .html/.css/.js files, served from
# /static. It used to be Python string literals here, and that coupling was
# the direct cause of two outages: a `\n` meant for JS became a real newline
# and broke the whole script, and editing JS through Python string surgery
# silently dropped a function definition while keeping its callers. Back end
# serves data and files; the front end is edited as front end.
WEB_DIR = REPO_ROOT / "web"

STATIC_TYPES = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}


def serve_static(handler: BaseHTTPRequestHandler, path: str) -> None:
    """Serve one file out of web/. Shared by every server in this project.

    The name is taken as a bare filename, never a path: a request is allowed
    to pick which asset it wants, not where to look for it.
    """

    name = Path(path).name
    target = WEB_DIR / name
    if not target.is_file() or target.parent != WEB_DIR:
        handler.send_error(404)
        return
    payload = target.read_bytes()
    mime = STATIC_TYPES.get(target.suffix, "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", mime)
    handler.send_header("Content-Length", str(len(payload)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(payload)


def page(label: str, interval_ms: int, *, bare: bool = False, writable: bool = False) -> bytes:
    """The viewer, live-polling; the shipped comparison samples are dropped
    when ``bare`` (a real work session has no use for them), and a real input
    box is appended when ``writable`` (there is somewhere for it to go)."""
    html = VIEWER.read_text(encoding="utf-8")
    if bare:
        html = re.sub(r"const SAMPLES = \{.*?\};?\n", "const SAMPLES = {};\n", html, count=1)
    # Drop the auto-pick so the shipped sample does not flash before the first
    # poll lands; the poller supplies the first load. The real call in
    # viewer.html is `pick(Object.keys(SAMPLES)[0]);` — bare mode empties
    # SAMPLES, so Object.keys(SAMPLES)[0] is undefined and an unstripped call
    # throws (TypeError: Cannot read properties of undefined) before the
    # poller ever runs. Caught by actually loading the page in a browser
    # (Playwright), not by the HTTP-level self-tests, which never execute
    # the page's JS and so never saw this.
    html = re.sub(r"pick\(Object\.keys\(SAMPLES\)\[0\]\)\s*;?", "", html, count=1)

    # Values, not code: the only thing the page needs from Python is what this
    # run is called and how often to poll. Everything executable is a file.
    settings = json.dumps(
        {"label": label, "interval": interval_ms, "writable": writable},
        ensure_ascii=False,
    )
    html += f'\n<script>window.__live = {settings};</script>\n'
    # Markdown first, and in read-only mode too: what the model wrote is a
    # table or a list whether or not this page can talk back, and viewer.html
    # only escapes it — which collapses every newline and turns a table into
    # one unreadable line. markdown.js wraps render(); it must be in place
    # before live.js polls and draws.
    html += f"<style>{(WEB_DIR / 'markdown.css').read_text(encoding='utf-8')}</style>\n"
    html += '<script src="/static/markdown.js"></script>\n'
    if writable:
        html += f"<style>{(WEB_DIR / 'live.css').read_text(encoding='utf-8')}</style>\n"
        html += (WEB_DIR / "live.html").read_text(encoding="utf-8")
    html += '<script src="/static/live.js"></script>\n'
    return html.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def __init__(
        self,
        *args,
        task_dir: Path,
        # bytes, or something that produces them per request. Building the
        # page once and holding it meant an edit to web/ was invisible until
        # the session was restarted — and restarting a session to see a CSS
        # change is exactly the coupling web/ exists to remove. multi_host
        # already builds it per request; this makes the two agree.
        body: bytes | Callable[[], bytes],
        push: Callable[[str], None] | None,
        recall_preview: Callable[[str], list[str]] | None = None,
        compress_status: Callable[[], dict] | None = None,
        model_status: Callable[[], dict] | None = None,
        model_switch: Callable[[dict], dict] | None = None,
        last_error: Callable[[], str | None] | None = None,
        streaming: Callable[[], dict] | None = None,
        interrupt: Callable[[], dict] | None = None,
        settings_read: Callable[[], dict] | None = None,
        settings_write: Callable[[dict], dict] | None = None,
        turn_action: Callable[[dict], dict] | None = None,
        **kwargs,
    ) -> None:
        self.task_dir = task_dir
        self.body = body
        self.push = push
        self.recall_preview = recall_preview
        self.compress_status = compress_status
        self.model_status = model_status
        self.model_switch = model_switch
        self.last_error = last_error
        self.streaming = streaming
        self.interrupt = interrupt
        self.settings_read = settings_read
        self.settings_write = settings_write
        self.turn_action = turn_action
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

    # URL path -> on-disk filename, for every fixed table besides history.jsonl
    # (which keeps its own branch below since it predates this map). Same
    # convention as history.jsonl: the default DEFAULT_TABLE_NAMES filename,
    # not whatever a task's YAML renamed it to — a task that renamed a table
    # would need this map extended, exactly as it would for history.jsonl.
    TABLE_FILES = {
        "/state.json": "state_latest.json",
        "/context.json": "context_latest.json",
        "/life_cycle.json": "life_cycle.json",
        "/patches.jsonl": "patches.jsonl",
        "/raw.jsonl": "raw_history.jsonl",
    }

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            body = self.body() if callable(self.body) else self.body
            self._send(body, "text/html; charset=utf-8")
        elif path.startswith("/static/"):
            serve_static(self, path)
        elif path == "/history.jsonl":
            history = self.task_dir / "history.jsonl"
            # A run that has not written yet is not an error: the page keeps
            # polling and picks it up when the first turn commits.
            data = history.read_bytes() if history.is_file() else b""
            self._send(data, "application/x-ndjson; charset=utf-8")
        elif path in self.TABLE_FILES:
            table = self.task_dir / self.TABLE_FILES[path]
            mime = "application/json" if path.endswith(".json") else "application/x-ndjson; charset=utf-8"
            default = b"{}" if path.endswith(".json") else b""
            data = table.read_bytes() if table.is_file() else default
            self._send(data, mime)
        elif path == "/snapshots":
            snapshots_dir = self.task_dir / "snapshots"
            names = (
                sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir())
                if snapshots_dir.is_dir()
                else []
            )
            self._send_json({"snapshots": names})
        elif path == "/compress/status":
            # IF-15: needs a live runtime (compression settings + in-memory
            # history/state), not just files on disk — None outside runtime.viewer.
            if self.compress_status is None:
                self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
                return
            self._send_json(self.compress_status())
        elif path == "/models":
            if self.model_status is None:
                self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
                return
            self._send_json(self.model_status())
        elif path == "/last-error":
            # Always 200, even unwired: the page polls this after every send,
            # and a 503 there would read as "the send failed" rather than
            # "this page has no runtime to report errors from".
            self._send_json({"error": self.last_error() if self.last_error else None})
        elif path == "/settings":
            if self.settings_read is None:
                self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
                return
            self._send_json(self.settings_read())
        elif path == "/tokens":
            self._send_json(self._token_stats())
        elif path == "/sessions":
            # The registry is shared state on disk, so a session can list its
            # siblings without any of them knowing about each other. Serving
            # it here (not only from the hub) is what lets the page offer a
            # switcher instead of making the user remember port numbers.
            from .session_registry import HUB_PORT, LAUNCHER_HINT, list_sessions

            self._send_json({
                "sessions": list(list_sessions().values()),
                "here": str(self.task_dir.name),
                "launcher": LAUNCHER_HINT,
                "hub": HUB_PORT,
            })
        elif path == "/streaming":
            # Partial output for the turn in flight. Always 200 for the same
            # reason /last-error is: the page polls this constantly, and a
            # 503 would read as a failure rather than "nothing to show".
            self._send_json(
                self.streaming() if self.streaming else {"text": "", "active": False}
            )
        else:
            self.send_error(404)

    # C 区 (IF-10~13): each just builds the same text a terminal user would
    # type and hands it to push — DatasetInputProcessor.next() already knows
    # how to parse /compat, /pin, /cancelpin, and the ContentPatch grammar
    # (code/dataset/input_data.py). No new runtime behavior, only validation
    # that turns a malformed request into a 400 instead of a silently
    # swallowed bad command.
    CONTROL_PATHS = frozenset({"/send", "/compress", "/pin", "/cancelpin", "/patch"})

    def _build_command(self, path: str, payload: dict) -> str:
        if path == "/send":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ValueError("empty text")
            return text
        if path == "/compress":
            return MANUAL_COMPRESS_COMMAND
        if path == "/pin":
            content = str(payload.get("content") or "").strip()
            if not content:
                raise ValueError("empty content")
            return f"/pin {content}"
        if path == "/cancelpin":
            pin_id = str(payload.get("pin_id") or "").strip()
            if not pin_id:
                raise ValueError("empty pin_id")
            return f"/cancelpin {pin_id}"
        if path == "/patch":
            element = str(payload.get("element") or "").strip()
            content = str(payload.get("content") or "").strip()
            mode = str(payload.get("mode") or "").strip()
            turns = payload.get("turns")
            if not element or "/" in element or " " in element:
                raise ValueError("element must be a single bare name")
            if not content:
                raise ValueError("empty content")
            if mode not in ("remain", "roll"):
                raise ValueError("mode must be 'remain' or 'roll'")
            if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
                raise ValueError("turns must be a positive integer")
            return f"/{element} {content} {mode} {turns}"
        raise KeyError(path)

    def _token_stats(self) -> dict:
        """How much text the ledger holds vs. how much is actually being sent.

        The gap between the two is the entire point of this runtime: history
        grows forever while context is a projection of it, so seeing both
        numbers side by side is what makes a life_cycle or a compaction
        legible. Counted from the same files the viewer already reads, so
        this works for a finished run as well as a live one.

        Tokens are estimated, not tokenized: every provider here uses a
        different tokenizer, and pulling one in to put a number on a status
        line would be a heavy dependency for a figure nobody bills against.
        CJK runs about a token per character, other text about four
        characters per token.
        """

        def estimate(text: str) -> int:
            cjk = sum(1 for ch in text if "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff")
            return cjk + max(0, (len(text) - cjk)) // 4

        history_chars = history_tokens = 0
        history_path = self.task_dir / "history.jsonl"
        if history_path.is_file():
            for line in history_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for slot in (row.get("content") or {}).values():
                    text = str((slot or {}).get("content") or "")
                    history_chars += len(text)
                    history_tokens += estimate(text)

        context_chars = context_tokens = context_messages = 0
        messages: list = []
        context_path = self.task_dir / "context_latest.json"
        if context_path.is_file():
            try:
                loaded = json.loads(context_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = []
            messages = loaded if isinstance(loaded, list) else []
            context_messages = len(messages)
            for message in messages:
                text = str((message or {}).get("content") or "")
                context_chars += len(text)
                context_tokens += estimate(text)

        state = _CACHE_STATE.setdefault(str(self.task_dir), {})
        previous = state.get("messages")
        if previous is not None and messages != previous:
            shared = 0
            for old, new in zip(previous, messages):
                if old != new:
                    break
                shared += estimate(str((new or {}).get("content") or ""))
            state["reused_tokens"] = shared
            state["total_tokens"] = context_tokens
            state["hit"] = round(100 * shared / context_tokens, 1) if context_tokens else 0.0
        if previous is None or messages != previous:
            state["messages"] = messages

        return {
            "history_chars": history_chars,
            "history_tokens": history_tokens,
            "context_chars": context_chars,
            "context_tokens": context_tokens,
            "context_messages": context_messages,
            # None until two different contexts have been observed — there is
            # no reuse figure for a turn with nothing to compare against.
            "cache_hit": state.get("hit"),
            "cache_reused_tokens": state.get("reused_tokens"),
        }

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/recall/preview":
            self._handle_recall_preview()
            return
        if path == "/model":
            self._handle_model_switch()
            return
        if path == "/interrupt":
            if self.interrupt is None:
                self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
                return
            self._send_json(self.interrupt())
            return
        if path == "/settings":
            if self.settings_write is None:
                self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._send_json({"error": "invalid JSON body"}, status=400)
                return
            try:
                self._send_json(self.settings_write(payload))
            except (ValueError, AssertionError) as exc:
                self._send_json({"error": str(exc)}, status=400)
            return
        if path == "/turn":
            self._handle_turn_action()
            return
        if path == "/new-run":
            from .launcher import ensure_launcher

            self._send_json({"url": ensure_launcher()})
            return
        if path not in self.CONTROL_PATHS:
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
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        try:
            text = self._build_command(path, payload)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        try:
            self.push(text)
        except Exception as exc:  # noqa: BLE001 - report it back to the page, don't 500 blindly
            self._send_json({"error": str(exc)}, status=500)
            return
        self._send_json({"ok": True})

    def _handle_recall_preview(self) -> None:
        # IF-14: runs recall["grep"] directly against an ad-hoc query, not
        # tied to the trigger that normally decides whether recall fires on a
        # real turn — needs the live runtime for the same reason
        # compress_status does, so None outside runtime.viewer.
        if self.recall_preview is None:
            self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
            query = str(payload.get("query") or "").strip()
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        if not query:
            self._send_json({"error": "empty query"}, status=400)
            return
        self._send_json({"results": self.recall_preview(query)})

    def _handle_turn_action(self) -> None:
        if self.turn_action is None:
            self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        try:
            self._send_json(self.turn_action(payload if isinstance(payload, dict) else {}))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def _handle_model_switch(self) -> None:
        if self.model_switch is None:
            self._send_json({"error": "此页面没有接到可写入的 runtime"}, status=503)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "invalid JSON body"}, status=400)
            return
        if not isinstance(payload, dict):
            self._send_json({"error": "body must be a JSON object"}, status=400)
            return
        try:
            self._send_json(self.model_switch(payload))
        except (ValueError, FileNotFoundError) as exc:
            # A rejected vendor/model/key is the caller's mistake, not a
            # server fault — and the provider was never swapped, so the run
            # keeps answering with whatever it had.
            self._send_json({"error": str(exc)}, status=400)

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
    recall_preview: Callable[[str], list[str]] | None = None,
    compress_status: Callable[[], dict] | None = None,
    model_status: Callable[[], dict] | None = None,
    model_switch: Callable[[dict], dict] | None = None,
    last_error: Callable[[], str | None] | None = None,
    streaming: Callable[[], dict] | None = None,
    interrupt: Callable[[], dict] | None = None,
    settings_read: Callable[[], dict] | None = None,
    settings_write: Callable[[dict], dict] | None = None,
    turn_action: Callable[[dict], dict] | None = None,
) -> ThreadingHTTPServer:
    if not VIEWER.is_file():
        raise SystemExit(f"{VIEWER} 不存在")
    task_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(
        Handler,
        task_dir=task_dir,
        body=partial(page, label, interval_ms, bare=bare, writable=push is not None),
        push=push,
        recall_preview=recall_preview,
        compress_status=compress_status,
        model_status=model_status,
        model_switch=model_switch,
        last_error=last_error,
        streaming=streaming,
        interrupt=interrupt,
        settings_read=settings_read,
        settings_write=settings_write,
        turn_action=turn_action,
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
    recall_preview: Callable[[str], list[str]] | None = None,
    compress_status: Callable[[], dict] | None = None,
    model_status: Callable[[], dict] | None = None,
    model_switch: Callable[[dict], dict] | None = None,
    last_error: Callable[[], str | None] | None = None,
    streaming: Callable[[], dict] | None = None,
    interrupt: Callable[[], dict] | None = None,
    settings_read: Callable[[], dict] | None = None,
    settings_write: Callable[[dict], dict] | None = None,
    turn_action: Callable[[dict], dict] | None = None,
) -> ThreadingHTTPServer:
    """Serve the live viewer for ``task_dir`` in a background thread.

    The caller decides whether a failure here (port taken, viewer.html
    missing) should be fatal — this only builds and starts the server, it
    never bounds the run's own turn loop.
    """
    server = build_server(
        task_dir,
        label=label,
        port=port,
        interval_ms=int(interval * 1000),
        bare=bare,
        push=push,
        recall_preview=recall_preview,
        compress_status=compress_status,
        model_status=model_status,
        model_switch=model_switch,
        last_error=last_error,
        streaming=streaming,
        interrupt=interrupt,
        settings_read=settings_read,
        settings_write=settings_write,
        turn_action=turn_action,
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


def _script_blocks(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def _assert_js_parses(_html: str = "") -> None:
    """Check the front-end files this project ships.

    The interactive layer is one script: a single broken construct takes all
    of it out at once and the page silently degrades to the read-only viewer,
    which is indistinguishable from "the feature was reverted". That has
    happened twice — once from a `\n` that became a real newline, once from an
    edit that dropped a function while keeping its callers.

    Now that web/ holds real files, they are checked as files. The argument is
    ignored and kept only so existing callers need not change.
    """

    import shutil
    import subprocess as _subprocess

    node = shutil.which("node")
    for source in sorted(WEB_DIR.glob("*.js")):
        text = source.read_text(encoding="utf-8")
        if node:
            result = _subprocess.run(
                [node, "--check", "-"], input=text, capture_output=True, text=True
            )
            if result.returncode != 0:
                raise AssertionError(f"{source.name} 不是合法 JS：{result.stderr.strip()[:200]}")
        _assert_no_undefined_calls(source.name, text)
    if node:
        _assert_markdown_renders(node)


# markdown.js is the one front-end file that is a pure function of its input,
# so it is the one that can actually be tested rather than only parsed: it
# exports mdToHtml on `this` when there is no window, which makes it a plain
# CommonJS module under node.
_MARKDOWN_PROBE = """
const md = require(process.argv[1]).mdToHtml;
const table = md("| a | b |\\n|---|---|\\n| 1 | 2 |");
if (!/<table>.*<td>1<\\/td>/s.test(table)) throw new Error("表格没渲染出来: " + table);
const para = md("first\\nsecond");
if (!para.includes("<br>")) throw new Error("换行被吃了: " + para);
if (md("<img src=x onerror=alert(1)>").includes("<img")) throw new Error("没转义");
if (!md('{"a":1}').includes("md-json")) throw new Error("JSON 没排版");
"""


def _assert_markdown_renders(node: str) -> None:
    import subprocess as _subprocess

    result = _subprocess.run(
        [node, "-e", _MARKDOWN_PROBE, str(WEB_DIR / "markdown.js")],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"markdown.js 渲染不对：{result.stderr.strip()[:300]}")


# Names the injected scripts may use without declaring them: browser builtins,
# plus what viewer.html itself defines above them.
_JS_GLOBALS = frozenset({
    "fetch", "setInterval", "clearInterval", "setTimeout", "clearTimeout",
    "parseInt", "parseFloat", "String", "Number", "Boolean", "Array", "Object",
    "JSON", "Math", "Date", "Error", "Set", "Map", "Promise", "RegExp",
    "document", "window", "location", "console", "isNaN", "alert", "confirm",
    "encodeURIComponent", "decodeURIComponent", "FileReader", "MutationObserver",
    # viewer.html's own, which the injected scripts are appended after.
    "load", "setTurn", "stop", "render", "parse", "pick", "readFile",
})

# Words that begin a block or expression and are followed by "(" — they read
# like calls to the pattern below but are syntax.
_JS_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "delete", "void", "in", "of", "do", "else", "await", "yield",
})


def _strip_js_literals(source: str) -> str:
    """Remove comments and string contents so prose cannot look like code.

    The page carries recorded sample conversations inside string literals, and
    a sentence such as "David (age 40)" matches a call pattern perfectly. Only
    executable text may be scanned.
    """

    # A scanner, not a regex. Quoting here is genuinely stateful — a double
    # quote inside a single-quoted string (`'<option value="'`) is data, not a
    # delimiter — and regex alternation gets the pairing wrong as soon as one
    # such quote appears, which inverted an earlier version of this: it kept
    # the string contents and deleted the code between them.
    out: list[str] = []
    quote: str | None = None
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
                out.append('""')
            index += 1
            continue
        if char in "\"'`":
            quote = char
            index += 1
            continue
        if char == "/" and index + 1 < length:
            following = source[index + 1]
            if following == "/":
                end = source.find("\n", index)
                index = length if end == -1 else end
                continue
            if following == "*":
                end = source.find("*/", index + 2)
                index = length if end == -1 else end + 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def _assert_no_undefined_calls(name: str, source: str) -> None:
    """Catch a call to a name the file never defines.

    `node --check` only parses; it cannot know that `attachLive()` refers to
    nothing. That is exactly how an edit shipped a page whose composer threw
    "Can't find variable" on the first click while every test still passed —
    the failure is at first call, not at parse.
    """

    stripped = _strip_js_literals(source)
    declared = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", stripped))
    declared |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)", stripped))
    declared |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*function", stripped))
    called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", stripped))
    missing = sorted(called - declared - _JS_GLOBALS - _JS_KEYWORDS)
    if missing:
        raise AssertionError(f"{name} 调用了未定义的名字：{missing}")


def _self_test() -> None:
    import tempfile
    import urllib.request

    # Bypass any system HTTP proxy: it has no business intercepting a
    # loopback request to a server this process just started.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with tempfile.TemporaryDirectory() as temporary:
        task_dir = Path(temporary)
        (task_dir / "history.jsonl").write_text('{"id":0}\n', encoding="utf-8")
        (task_dir / "state_latest.json").write_text('{"0":{"system":1}}', encoding="utf-8")
        (task_dir / "context_latest.json").write_text(
            '[{"role":"system","content":"hi"}]', encoding="utf-8"
        )
        (task_dir / "life_cycle.json").write_text('{"system":[1,null]}', encoding="utf-8")
        (task_dir / "patches.jsonl").write_text('{"turn":1,"element":"goal"}\n', encoding="utf-8")
        (task_dir / "raw_history.jsonl").write_text('{"id":1,"raw":"<think>1</think>"}\n', encoding="utf-8")
        (task_dir / "snapshots" / "turn_0002").mkdir(parents=True)
        (task_dir / "snapshots" / "turn_0001").mkdir(parents=True)

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
            _assert_js_parses(body.decode("utf-8"))
            with opener.open(f"http://127.0.0.1:{port}/history.jsonl") as response:
                data = response.read()
            assert data == b'{"id":0}\n'

            # R 区: IF-04~09, plain reads of the other fixed tables.
            with opener.open(f"http://127.0.0.1:{port}/state.json") as response:
                assert json.loads(response.read()) == {"0": {"system": 1}}
            with opener.open(f"http://127.0.0.1:{port}/context.json") as response:
                assert json.loads(response.read()) == [{"role": "system", "content": "hi"}]
            with opener.open(f"http://127.0.0.1:{port}/life_cycle.json") as response:
                assert json.loads(response.read()) == {"system": [1, None]}
            with opener.open(f"http://127.0.0.1:{port}/patches.jsonl") as response:
                assert response.read() == b'{"turn":1,"element":"goal"}\n'
            with opener.open(f"http://127.0.0.1:{port}/raw.jsonl") as response:
                assert response.read() == b'{"id":1,"raw":"<think>1</think>"}\n'
            with opener.open(f"http://127.0.0.1:{port}/snapshots") as response:
                assert json.loads(response.read()) == {"snapshots": ["turn_0001", "turn_0002"]}

            # A table that hasn't been written yet is a default value, not a 404.
            with tempfile.TemporaryDirectory() as empty_temporary:
                empty_dir = Path(empty_temporary)
                empty_server = build_server(empty_dir, label="empty", port=0, interval_ms=500)
                empty_thread = threading.Thread(target=empty_server.serve_forever, daemon=True)
                empty_thread.start()
                try:
                    empty_port = empty_server.server_address[1]
                    with opener.open(f"http://127.0.0.1:{empty_port}/state.json") as response:
                        assert json.loads(response.read()) == {}
                    with opener.open(f"http://127.0.0.1:{empty_port}/patches.jsonl") as response:
                        assert response.read() == b""
                    with opener.open(f"http://127.0.0.1:{empty_port}/snapshots") as response:
                        assert json.loads(response.read()) == {"snapshots": []}
                finally:
                    empty_server.shutdown()
                    empty_server.server_close()
                    empty_thread.join(timeout=2)

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

            # N 区 (IF-14/15): unwired outside runtime.viewer -> 503, not a crash.
            try:
                opener.open(f"http://127.0.0.1:{port}/compress/status")
                raise AssertionError("compress_status 未接入时应该失败")
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
            preview_request = urllib.request.Request(
                f"http://127.0.0.1:{port}/recall/preview",
                data=b'{"query":"x"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                opener.open(preview_request)
                raise AssertionError("recall_preview 未接入时应该失败")
            except urllib.error.HTTPError as exc:
                assert exc.code == 503
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        # Writable mode (runtime.viewer's use): bare page, /send reaches push.
        received: list[str] = []
        switched: list[dict] = []

        def _fake_switch(payload: dict) -> dict:
            if payload.get("provider") == "nope":
                raise ValueError("unsupported provider 'nope'")
            switched.append(payload)
            return {"ok": True, "provider": payload.get("provider"), "model": payload.get("model")}

        server = build_server(
            task_dir,
            label="live",
            port=0,
            interval_ms=500,
            bare=True,
            push=received.append,
            recall_preview=lambda query: [f"echo:{query}"],
            compress_status=lambda: {"enabled": True, "current_turn": 3},
            model_status=lambda: {
                "current": {"provider": "ollama", "model": "qwen3:8b"},
                "vendors": {"ollama": {"base_url": "http://127.0.0.1:11434", "model": "qwen3:8b"}},
                "saved": [],
            },
            model_switch=_fake_switch,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with opener.open(f"http://127.0.0.1:{port}/") as response:
                body = response.read()
            assert b"const SAMPLES = {};" in body
            assert b"live-send" in body
            # The interactive layer is one script; if it does not parse the
            # page silently loses every feature at once.
            _assert_js_parses(body.decode("utf-8"))
            def post(sub_path: str, body: dict) -> tuple[int, dict]:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}{sub_path}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with opener.open(request) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            # N 区: IF-14/15, wired to fake runtime-bound callables.
            with opener.open(f"http://127.0.0.1:{port}/compress/status") as response:
                assert json.loads(response.read()) == {"enabled": True, "current_turn": 3}
            status, reply = post("/recall/preview", {"query": "花生"})
            assert (status, reply) == (200, {"results": ["echo:花生"]})
            status, reply = post("/recall/preview", {"query": "  "})
            assert status == 400 and "error" in reply

            # Model picker: list, switch, and a rejected switch.
            with opener.open(f"http://127.0.0.1:{port}/models") as response:
                models = json.loads(response.read())
            assert models["current"]["provider"] == "ollama"
            assert "ollama" in models["vendors"]
            status, reply = post("/model", {"provider": "glm", "model": "GLM-4.5-Air"})
            assert status == 200 and reply["ok"] is True
            assert switched[-1]["model"] == "GLM-4.5-Air"
            status, reply = post("/model", {"provider": "nope"})
            assert status == 400 and "error" in reply

            status, reply = post("/send", {"text": "hello from the page"})
            assert (status, reply) == (200, {"ok": True})
            assert received == ["hello from the page"]

            # C 区: IF-10~13, each just builds the equivalent command text.
            status, reply = post("/compress", {})
            assert (status, reply) == (200, {"ok": True})
            assert received[-1] == MANUAL_COMPRESS_COMMAND

            status, reply = post("/pin", {"content": "示例事实一"})
            assert (status, reply) == (200, {"ok": True})
            assert received[-1] == "/pin 示例事实一"

            status, reply = post("/cancelpin", {"pin_id": "p01"})
            assert (status, reply) == (200, {"ok": True})
            assert received[-1] == "/cancelpin p01"

            status, reply = post(
                "/patch", {"element": "goal", "content": "ship it", "mode": "roll", "turns": 5}
            )
            assert (status, reply) == (200, {"ok": True})
            assert received[-1] == "/goal ship it roll 5"

            # Validation: malformed control requests 400 instead of reaching push.
            before = len(received)
            status, reply = post("/pin", {"content": "  "})
            assert status == 400 and "error" in reply
            status, reply = post("/patch", {"element": "goal", "content": "x", "mode": "bogus", "turns": 1})
            assert status == 400 and "error" in reply
            status, reply = post("/patch", {"element": "goal", "content": "x", "mode": "roll", "turns": 0})
            assert status == 400 and "error" in reply
            status, reply = post("/patch", {"element": "has space", "content": "x", "mode": "roll", "turns": 1})
            assert status == 400 and "error" in reply
            assert len(received) == before  # nothing invalid ever reached push
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    _self_test()
    print("saver.live_view: ok")
