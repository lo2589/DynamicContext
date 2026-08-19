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
        try {
          load(text, label + ' · ' + turns + ' rows · live')
          // load() ends on setTurn(0). For a recorded sample that is the
          // right place to start; for a live run it means every committed
          // turn snaps the view back to turn 0, so the reply you just asked
          // for is written to the ledger and never shown. Follow the newest
          // turn instead — the whole point of watching a run as it happens.
          if (model && model.turns.length) setTurn(model.turns.length - 1)
        } catch (e) {}
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

# Appended only when a `push` callback is wired up server-side. The composer
# is moved into the Conversation pane (right under #chat) at load: an input
# box belongs with the transcript it writes into, not stranded at the page
# bottom. It is built outside the #chat element that load()/render() rewrite
# on every tick, so a re-render never clobbers it — hence appending to the
# pane rather than into the chat scroller.
SEND_BAR = """
<style>
#live-send{display:flex;flex-direction:column;gap:8px;
  padding:10px 14px;border-top:1px solid var(--rule);background:var(--panel)}
#live-send .row{display:flex;gap:8px;align-items:flex-end}
#live-send input,#live-send select,#live-send textarea{padding:7px 9px;border:1px solid var(--rule);
  border-radius:6px;background:var(--paper);color:var(--ink);font:inherit;font-size:12.5px;min-width:0}
#live-send-text{flex:1;resize:vertical;font-family:ui-sans-serif,system-ui,sans-serif;line-height:1.5}
#live-send button{padding:7px 13px;border:1px solid var(--rule);
  border-radius:6px;background:var(--live);color:var(--paper);cursor:pointer;
  font:inherit;font-size:12.5px;white-space:nowrap}
#live-send button:disabled{opacity:.5;cursor:default}
#live-send .status{color:var(--ink-2);font-size:11.5px;min-width:7em}
/* The in-progress reply is a message, so it is rendered as one, in the
   transcript, using the viewer's own bubble classes — .b.t for reasoning and
   .b.a for the answer, exactly as a committed turn looks. */
#live-provisional{display:flex;flex-direction:column;gap:9px;padding:0 14px 6px}
#live-provisional .b{animation:livepulse 1.4s ease-in-out infinite}
@keyframes livepulse{0%,100%{opacity:.62}50%{opacity:1}}
#live-model{color:var(--ink-2);font-size:11px;letter-spacing:.04em;
  display:flex;gap:7px;align-items:center;flex-wrap:wrap}
#live-model b{color:var(--ink);font-weight:600;letter-spacing:0}
#live-model button{background:transparent;color:var(--live);border-color:var(--rule);
  padding:4px 9px;font-size:11px}
#live-model-form{display:none;gap:7px;align-items:center;flex-wrap:wrap;width:100%}
#live-model-form.open{display:flex}
#live-model-form input{flex:1;min-width:9rem;font-size:11.5px;padding:5px 8px}
#live-model-form select{font-size:11.5px;padding:5px 8px}
</style>
<div id="live-send">
  <div id="live-model">
    <span>model</span><b id="live-model-now">…</b>
    <button id="live-model-toggle" type="button">change</button>
    <div id="live-model-form">
      <select id="live-model-saved"><option value="">— 新配置 —</option></select>
      <select id="live-model-vendor"></select>
      <select id="live-model-name"></select>
      <input id="live-model-name-manual" placeholder="model 名字" autocomplete="off" hidden>
      <input id="live-model-url" placeholder="base_url" autocomplete="off">
      <input id="live-model-key" type="password" placeholder="api_key（本地保存）" autocomplete="off">
      <input id="live-model-saveas" placeholder="存成 xxx.json（可留空）" autocomplete="off">
      <button id="live-model-apply" type="button">Apply</button>
      <span class="status" id="live-model-status"></span>
    </div>
  </div>
  <div class="row">
    <textarea id="live-send-text" rows="2" placeholder="跟这个 run 说点什么…（回车换行，⌘/Ctrl+回车 或点 Send 发送）"></textarea>
    <button id="live-send-btn">Send</button>
    <span class="status" id="live-send-status"></span>
  </div>
</div>
<script>
(function () {
  var bar = document.getElementById("live-send")
  var chat = document.getElementById("chat")
  // Sit inside the Conversation pane, directly after the transcript.
  if (chat && chat.parentNode) chat.parentNode.appendChild(bar)

  var input = document.getElementById("live-send-text")
  var button = document.getElementById("live-send-btn")
  var status = document.getElementById("live-send-status")
  // Provisional bubbles live in the transcript, after it. render() rewrites
  // #chat only when new data lands, and when it does this turn has committed
  // — so the placeholder is replaced by the real thing at exactly the right
  // moment, with no cleanup race.
  var live = document.createElement("div")
  live.id = "live-provisional"
  if (chat && chat.parentNode) chat.parentNode.insertBefore(live, bar)

  // The stream carries the model's raw markup (<think>…</think> then the
  // answer). Split it the same way the ledger will, so what is shown while
  // generating matches what is shown once committed.
  function renderLive(text) {
    if (!text) { live.innerHTML = ""; return }
    var think = "", answer = text
    var open = text.indexOf("<think>")
    if (open !== -1) {
      var close = text.indexOf("</think>")
      if (close === -1) { think = text.slice(open + 7); answer = "" }
      else { think = text.slice(open + 7, close); answer = text.slice(close + 8) }
    }
    var html = ""
    if (think.trim()) {
      html += '<div class="b t"><span class="tag">think · generating</span>' +
        escapeHtml(think.trim().slice(-400)) + "</div>"
    }
    if (answer.trim()) {
      html += '<div class="b a"><span class="tag">assistant · generating</span>' +
        escapeHtml(answer.trim()) + "</div>"
    }
    live.innerHTML = html
    live.scrollIntoView({ block: "end" })
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  function send() {
    var text = input.value.trim()
    if (!text) return
    var sent = text  // kept so a failed turn can hand the draft back
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
        var started = Date.now()
        var sawActive = false
        function finish(message) {
          clearInterval(fast)
          status.textContent = message || ""
          renderLive("")
          window.__liveTick()  // make sure the committed turn is on screen
        }
        var fast = setInterval(function () {
          tries += 1
          if (tries > 400) { finish("no reply"); return }
          // Show generation as it arrives — the terminal has always had this
          // via on_chunk; without it a local model looks like a frozen page
          // for the tens of seconds it takes to answer.
          fetch("/streaming", { cache: "no-store" })
            .then(function (r) { return r.ok ? r.json() : null })
            .then(function (s) {
              if (!s) return
              if (s.active) {
                sawActive = true
                var secs = Math.round((Date.now() - started) / 1000)
                status.textContent = "generating " + secs + "s"
                renderLive(s.text)
              } else if (sawActive) {
                // Generation ended. This — not the history poll — is the
                // authoritative finish signal: the background poller runs on
                // its own interval and may consume the history change first,
                // in which case __liveTick() here reports "nothing new" and
                // the placeholder would never be cleared.
                finish("")
              }
            })
            .catch(function () {})
          window.__liveTick().then(function (changed) {
            if (changed) { finish(""); return }
            // No new turn yet — it may simply be slow, or the turn may have
            // failed outright (bad key, unreachable model). The runtime
            // records why; without checking, a failed turn is indistinguishable
            // from a slow one and the box just spins forever.
            return fetch("/last-error", { cache: "no-store" })
              .then(function (r) { return r.ok ? r.json() : null })
              .then(function (d) {
                if (d && d.error) {
                  finish(d.error)
                  // The turn was never committed, so the words are gone
                  // unless we hand them back. Only restore into an empty box
                  // — never clobber something typed while waiting.
                  if (!input.value) input.value = sent
                }
              })
              .catch(function () {})
          })
        }, 750)
      })
      .catch(function (err) { status.textContent = "failed: " + err.message })
      .then(function () { button.disabled = false })
  }

  button.onclick = send
  // Enter inserts a newline; it must never send. A model can take tens of
  // seconds, so an accidental Enter looked exactly like "my text vanished" —
  // the box cleared and nothing came back for a long while. Sending is now
  // always deliberate: the button, or an explicit modifier+Enter.
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send() }
  })

  // ---- model picker ----
  var now = document.getElementById("live-model-now")
  var form = document.getElementById("live-model-form")
  var toggle = document.getElementById("live-model-toggle")
  var vendorSel = document.getElementById("live-model-vendor")
  var savedSel = document.getElementById("live-model-saved")
  var nameIn = document.getElementById("live-model-name")
  var urlIn = document.getElementById("live-model-url")
  var keyIn = document.getElementById("live-model-key")
  var saveAsIn = document.getElementById("live-model-saveas")
  var applyBtn = document.getElementById("live-model-apply")
  var modelStatus = document.getElementById("live-model-status")
  var nameManual = document.getElementById("live-model-name-manual")
  var vendors = {}
  var installed = []
  var MANUAL = "__manual__"

  // What the model field currently means, wherever it is being read from.
  function chosenModel() {
    return nameIn.value === MANUAL ? nameManual.value.trim() : nameIn.value
  }

  function refresh() {
    return fetch("/models", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (data) {
        if (!data) return
        vendors = data.vendors || {}
        installed = data.installed || []
        now.textContent = data.current.provider + " / " + data.current.model
        rebuildModels(data.current.model)
        if (!vendorSel.options.length) {
          Object.keys(vendors).forEach(function (v) {
            var o = document.createElement("option")
            o.value = v; o.textContent = v
            vendorSel.appendChild(o)
          })
          vendorSel.value = data.current.provider
          fillDefaults()
        }
        // Rebuild the saved-config list; a just-saved file must show up.
        var keep = savedSel.value
        savedSel.innerHTML = '<option value="">— 新配置 —</option>'
        ;(data.saved || []).forEach(function (f) {
          var o = document.createElement("option")
          o.value = f; o.textContent = f
          savedSel.appendChild(o)
        })
        savedSel.value = keep
      })
      .catch(function () {})
  }

  // A real dropdown of what is actually installed. Ollama tags must match
  // exactly, so typing one by hand is the error-prone path — it stays
  // available under "手动输入…" for vendors whose catalogue we cannot list.
  function rebuildModels(selected) {
    var options = vendorSel.value === "ollama" ? installed.slice() : []
    var preferred = vendors[vendorSel.value] && vendors[vendorSel.value].model
    if (preferred && options.indexOf(preferred) === -1) options.unshift(preferred)
    if (selected && options.indexOf(selected) === -1) options.unshift(selected)
    nameIn.innerHTML = options.map(function (m) {
      return '<option value="' + m + '">' + m + "</option>"
    }).join("") + '<option value="' + MANUAL + '">手动输入…</option>'
    nameIn.value = selected && options.indexOf(selected) !== -1 ? selected : (options[0] || MANUAL)
    syncManual()
  }

  function syncManual() {
    nameManual.hidden = nameIn.value !== MANUAL
  }

  function fillDefaults() {
    var d = vendors[vendorSel.value] || {}
    urlIn.value = d.base_url || ""
    rebuildModels(d.model || "")
  }

  toggle.onclick = function () { form.classList.toggle("open") }
  vendorSel.onchange = fillDefaults
  nameIn.onchange = syncManual
  savedSel.onchange = function () {
    // Picking a saved config means "use this one as-is"; the manual fields
    // stop applying, so grey them out rather than pretend they still matter.
    var usingSaved = !!savedSel.value
    ;[vendorSel, nameIn, nameManual, urlIn, keyIn, saveAsIn].forEach(function (el) { el.disabled = usingSaved })
  }

  applyBtn.onclick = function () {
    applyBtn.disabled = true
    modelStatus.textContent = "switching…"
    var body = savedSel.value
      ? { use_saved: savedSel.value }
      : {
          provider: vendorSel.value,
          model: chosenModel(),
          base_url: urlIn.value.trim(),
          api_key: keyIn.value,
          save_as: saveAsIn.value.trim(),
        }
    fetch("/model", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.statusText); return d }) })
      .then(function (d) {
        keyIn.value = ""
        return refresh().then(function () {
          // The switch is done and the header above already shows the new
          // model — leaving the form open with a stale "switched" label just
          // looks stuck. Collapse it; the header is the confirmation.
          form.classList.remove("open")
          modelStatus.textContent = ""
          var head = document.getElementById("live-model-now")
          head.textContent += d.saved_to ? "  (saved)" : ""
        })
      })
      .catch(function (err) { modelStatus.textContent = "failed: " + err.message })
      .then(function () { applyBtn.disabled = false })
  }

  refresh()
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
    # poll lands; the poller supplies the first load. The real call in
    # viewer.html is `pick(Object.keys(SAMPLES)[0]);` — bare mode empties
    # SAMPLES, so Object.keys(SAMPLES)[0] is undefined and an unstripped call
    # throws (TypeError: Cannot read properties of undefined) before the
    # poller ever runs. Caught by actually loading the page in a browser
    # (Playwright), not by the HTTP-level self-tests, which never execute
    # the page's JS and so never saw this.
    html = re.sub(r"pick\(Object\.keys\(SAMPLES\)\[0\]\)\s*;?", "", html, count=1)
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
        recall_preview: Callable[[str], list[str]] | None = None,
        compress_status: Callable[[], dict] | None = None,
        model_status: Callable[[], dict] | None = None,
        model_switch: Callable[[dict], dict] | None = None,
        last_error: Callable[[], str | None] | None = None,
        streaming: Callable[[], dict] | None = None,
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
            self._send(self.body, "text/html; charset=utf-8")
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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/recall/preview":
            self._handle_recall_preview()
            return
        if path == "/model":
            self._handle_model_switch()
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
) -> ThreadingHTTPServer:
    if not VIEWER.is_file():
        raise SystemExit(f"{VIEWER} 不存在")
    task_dir.mkdir(parents=True, exist_ok=True)
    handler = partial(
        Handler,
        task_dir=task_dir,
        body=page(label, interval_ms, bare=bare, writable=push is not None),
        push=push,
        recall_preview=recall_preview,
        compress_status=compress_status,
        model_status=model_status,
        model_switch=model_switch,
        last_error=last_error,
        streaming=streaming,
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
