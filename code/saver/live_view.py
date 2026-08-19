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
#live-stop{background:transparent;color:var(--mark);border-color:var(--mark)}
#live-settings{margin-left:6px;padding:3px 9px;border:1px solid var(--rule);border-radius:6px;
  background:transparent;color:var(--ink-2);font:inherit;font-size:11px;cursor:pointer}
#live-settings:hover{border-color:var(--live);color:var(--live)}
#live-settings-panel{display:none;flex-direction:column;gap:7px;padding:11px 14px;
  border-top:1px solid var(--rule);background:var(--panel);font-size:12px}
#live-settings-panel.open{display:flex}
#live-settings-panel .hint{color:var(--ink-2);font-size:11px;line-height:1.5}
.sc-row{display:grid;grid-template-columns:7rem 1fr 4rem;gap:7px;align-items:center}
.sc-row select,.sc-row input{padding:5px 7px;border:1px solid var(--rule);border-radius:6px;
  background:var(--paper);color:var(--ink);font:inherit;font-size:11.5px}
#live-settings-apply{align-self:flex-start;padding:6px 13px;border:1px solid var(--live);
  border-radius:6px;background:var(--live);color:var(--paper);font:inherit;font-size:12px;cursor:pointer}
#live-send .status{color:var(--ink-2);font-size:11.5px;min-width:7em}
/* The in-progress reply is a message, so it is rendered as one, in the
   transcript, using the viewer's own bubble classes — .b.t for reasoning and
   .b.a for the answer, exactly as a committed turn looks. */
#live-provisional{display:flex;flex-direction:column;gap:9px}
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
/* Session controls and the token readout live in the Conversation pane's own
   heading: that row already says what this pane is showing, and switching
   runs or starting one is the same kind of statement about it. */
#live-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;
  letter-spacing:0;text-transform:none;font-size:11.5px;color:var(--ink-2)}
#live-session{font-size:11.5px;padding:3px 7px;max-width:13rem;border:1px solid var(--rule);
  border-radius:6px;background:var(--paper);color:var(--ink);font-family:inherit}
#live-new{padding:3px 9px;border:1px solid var(--live);border-radius:6px;
  background:transparent;color:var(--live);font:inherit;font-size:11px;cursor:pointer}
#live-new:hover{background:var(--live);color:var(--paper)}
#live-new:disabled{opacity:.5;cursor:default}
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
    <button id="live-stop" type="button" hidden>停止</button>
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
  var stopBtn = document.getElementById("live-stop")
  stopBtn.onclick = function () {
    stopBtn.disabled = true
    // The runtime treats a stop as a successful partial generation, so what
    // was produced up to here is parsed and committed like any other turn.
    fetch("/interrupt", { method: "POST" })
      .then(function (r) { return r.json() })
      .then(function (d) { if (!d.ok) status.textContent = d.reason || "停不了" })
      .catch(function () {})
  }

  // Provisional bubbles go inside the transcript, as its last children.
  // Sitting outside it put the message you just sent in a separate block
  // below the conversation — which is where "my input showed up at the
  // bottom" came from. render() rewrites #chat wholesale, so re-attach on
  // every draw rather than placing it once.
  var live = document.createElement("div")
  live.id = "live-provisional"

  function attachLive() {
    if (chat && live.parentNode !== chat) chat.appendChild(live)
  }
  attachLive()

  // The stream carries the model's raw markup (<think>…</think> then the
  // answer). Split it the same way the ledger will, so what is shown while
  // generating matches what is shown once committed.
  // The question being answered has not been committed yet, so it is not in
  // #chat — without echoing it here the reply's bubbles appear above the
  // message that prompted them, which reads as the model answering before
  // being asked. pendingUser is cleared together with the bubbles, at the
  // moment the committed turn (which contains the real user slot) lands.
  var pendingUser = ""

  function renderLive(text) {
    if (!text && !pendingUser) { live.innerHTML = ""; return }
    attachLive()
    var think = "", answer = text || ""
    var open = text.indexOf("<think>")
    if (open !== -1) {
      var close = text.indexOf("</think>")
      if (close === -1) { think = text.slice(open + 7); answer = "" }
      else { think = text.slice(open + 7, close); answer = text.slice(close + 8) }
    }
    var html = ""
    if (pendingUser) {
      html += '<div class="b u">' + escapeHtml(pendingUser) + "</div>"
    }
    if (think.trim()) {
      html += '<div class="b t"><span class="tag">think · generating</span>' +
        escapeHtml(think.trim().slice(-400)) + "</div>"
    }
    if (answer.trim()) {
      html += '<div class="b a"><span class="tag">assistant · generating</span>' +
        escapeHtml(answer.trim()) + "</div>"
    }
    attachLive()
    live.innerHTML = html
    if (chat) chat.scrollTop = chat.scrollHeight
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
        pendingUser = sent
        renderLive("")
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
          pendingUser = ""
          stopBtn.hidden = true
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
                stopBtn.hidden = false
                stopBtn.disabled = false
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

  // ---- session switcher (top of page) ----
  var head = document.createElement("span")
  head.id = "live-head"
  head.innerHTML = '<select id="live-session"></select>' +
    '<button id="live-new" type="button">＋ 新对话</button>' +
    '<button id="live-settings" type="button">设置</button>'
  // The Conversation pane's heading — the row that already reads
  // "Conversation · N slots · M exchanges".
  var h2 = chat && chat.parentNode ? chat.parentNode.querySelector("h2") : null
  if (h2) h2.appendChild(head)

  var sessionSel = document.getElementById("live-session")

  function refreshSessions() {
    return fetch("/sessions", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (d) {
        if (!d) return
        if (d.launcher) window.__launcherPort = d.launcher
        if (d.hub) window.__hubPort = d.hub
        var here = d.here
        var html = ""
        var seen = false
        ;(d.sessions || []).forEach(function (s) {
          var mine = s.task === here
          if (mine) seen = true
          html += '<option value="' + s.port + '"' + (mine ? " selected" : "") + ">" +
            s.task + (mine ? "（这个）" : "") + "</option>"
        })
        if (!seen) html = '<option value="" selected>' + here + "（这个）</option>" + html
        // No "new run" entry here: the button beside this select already does
        // it, and offering the same action twice in one row is just noise.
        sessionSel.innerHTML = html
      })
      .catch(function () {})
  }

  sessionSel.onchange = function () {
    if (sessionSel.value) location.href = "http://127.0.0.1:" + sessionSel.value + "/"
  }

  // The stats row already answers "how big is this run" in slots; tokens and
  // prefix reuse answer the same question in the unit a provider bills and
  // caches by, so they belong in that row rather than squeezed into a
  // heading. Built with the page's own markup so they inherit its styling.
  var statsRow = document.querySelector(".stats")
  var tokensOut = null
  if (statsRow) {
    var extra = document.createElement("span")
    extra.id = "live-token-stats"
    extra.style.display = "contents"
    extra.innerHTML =
      '<span>history tokens<b id="lt-history">0</b></span>' +
      '<span>context tokens<b id="lt-context">0</b></span>' +
      '<span>prefix cache<b id="lt-cache">—</b></span>'
    statsRow.appendChild(extra)
    tokensOut = extra
  }

  function refreshTokens() {
    return fetch("/tokens", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (d) {
        if (!d) return
        var hit = (d.cache_hit === null || d.cache_hit === undefined) ? "—" : d.cache_hit + "%"
        document.getElementById("lt-history").textContent = fmt(d.history_tokens)
        document.getElementById("lt-context").textContent = fmt(d.context_tokens)
        document.getElementById("lt-cache").textContent = hit
        tokensOut.title =
          "history " + d.history_chars + " 字符 / 约 " + d.history_tokens + " token（账本里写下的全部内容）\\n" +
          "context " + d.context_chars + " 字符 / 约 " + d.context_tokens + " token（本轮真正送给模型的）\\n" +
          d.context_messages + " 条消息\\n" +
          "cache：本轮 context 与上一轮相同的前缀占比（" + (d.cache_reused_tokens || 0) +
          " token 可复用）。退役一条靠前的槽位会让前缀失配，命中率掉下来。\\n" +
          "token 为估算，非精确分词"
      })
      .catch(function () {})
  }

  function fmt(n) {
    return n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n)
  }

  var newBtn = document.getElementById("live-new")
  newBtn.onclick = function () {
    newBtn.disabled = true
    newBtn.textContent = "开面板…"
    // The panel may not be running; the server starts it if needed and only
    // answers once it is actually listening.
    fetch("/new-run", { method: "POST" })
      .then(function (r) { return r.json() })
      .then(function (d) { location.href = d.url })
      .catch(function () { newBtn.disabled = false; newBtn.textContent = "＋ 新对话" })
  }

  refreshTokens()
  refreshSessions()
  // Sessions come and go while this page is open; a stale list would send
  // the user to a dead port.
  setInterval(refreshSessions, 5000)
  // Token counts change with every commit, so follow the same cadence as the
  // transcript rather than a slow independent timer.
  setInterval(refreshTokens, 2000)

  // ---- settings: change the declarations of a running conversation ----
  var settingsBtn = document.getElementById("live-settings")
  var panel = document.createElement("div")
  panel.id = "live-settings-panel"
  panel.innerHTML =
    '<div class="hint">改声明不动账本：history 一个字不变，整张表按新规则从第 0 轮重算。' +
    '这正是 state = π(流, 声明) 的意思。</div>' +
    '<div id="sc-rows"></div>' +
    '<button id="live-settings-apply" type="button">应用并重算</button>' +
    '<span class="status" id="sc-status"></span>'
  if (chat && chat.parentNode) chat.parentNode.insertBefore(panel, bar)

  var RULES = [
    {id: "", label: "一直在场"},
    {id: "born", label: "只在出生那轮"},
    {id: "born+n", label: "出生后再留 n 轮"},
    {id: "until_cancelled", label: "钉住直到取消"},
    {id: "cycle", label: "被指回时缺席"},
    {id: "labelled", label: "按数据集标记"},
    {id: "until_goal_end", label: "直到 goal 结束"},
  ]
  var declared = {}

  function ruleOf(end) {
    if (end === null || end === undefined || end === "") return {rule: "", n: 3}
    if (typeof end === "string" && end.indexOf("born+") === 0) {
      return {rule: "born+n", n: parseInt(end.slice(5), 10) || 3}
    }
    return {rule: String(end), n: 3}
  }

  function renderSettings() {
    var host = document.getElementById("sc-rows")
    host.innerHTML = ""
    Object.keys(declared).forEach(function (element) {
      var bound = declared[element]
      // system is turn 0's slot, declared as a literal range rather than by
      // an end-function; editing it here would be a category error.
      if (element === "system") return
      var picked = ruleOf(bound[1])
      var row = document.createElement("div")
      row.className = "sc-row"
      row.innerHTML = "<span>" + element + "</span>" +
        "<select>" + RULES.map(function (r) {
          return '<option value="' + r.id + '"' + (r.id === picked.rule ? " selected" : "") +
            ">" + r.label + "</option>"
        }).join("") + "</select>" +
        '<input type="number" min="1" value="' + picked.n + '"' +
          (picked.rule === "born+n" ? "" : " hidden") + ">"
      var sel = row.querySelector("select")
      var num = row.querySelector("input")
      sel.onchange = function () {
        num.hidden = sel.value !== "born+n"
        declared[element] = [bound[0], boundValue(sel.value, num.value)]
        renderSettings()
      }
      num.oninput = function () {
        declared[element] = [bound[0], boundValue(sel.value, num.value)]
      }
      host.appendChild(row)
    })
  }

  function boundValue(rule, n) {
    if (!rule) return null
    if (rule === "born+n") return "born+" + Math.max(1, parseInt(n || "1", 10))
    return rule
  }

  settingsBtn.onclick = function () {
    panel.classList.toggle("open")
    if (!panel.classList.contains("open")) return
    fetch("/settings", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (d) {
        if (!d) return
        declared = d.life_cycle || {}
        renderSettings()
      })
      .catch(function () {})
  }

  document.getElementById("live-settings-apply").onclick = function () {
    var out = document.getElementById("sc-status")
    out.textContent = "重算中…"
    fetch("/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ life_cycle: declared }),
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error); return d }) })
      .then(function (d) {
        out.textContent = "已重算：" + d.visible + " 个槽位在场 · context " + d.context_messages + " 条"
        window.__liveTick()
        refreshTokens()
      })
      .catch(function (e) { out.textContent = "失败：" + e.message })
  }

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
        interrupt: Callable[[], dict] | None = None,
        settings_read: Callable[[], dict] | None = None,
        settings_write: Callable[[dict], dict] | None = None,
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
        interrupt=interrupt,
        settings_read=settings_read,
        settings_write=settings_write,
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


def _assert_js_parses(html: str) -> None:
    """Every injected <script> must at least be lexically intact.

    This page's whole interactive layer — composer, session switcher, model
    picker, streaming — lives in one injected script, so a single broken
    string literal takes all of it out at once and the page silently
    degrades to the read-only viewer. That is indistinguishable from "the
    feature was reverted", and it has happened: a `\n` written into the
    Python template became a real newline in the served JS, leaving an
    unterminated string.

    Node is used when present (a real parse); otherwise fall back to
    checking that no string literal spans a line break, which is exactly the
    failure mode the template makes easy.
    """

    import shutil
    import subprocess as _subprocess

    for index, block in enumerate(_script_blocks(html)):
        node = shutil.which("node")
        if node:
            result = _subprocess.run(
                [node, "--check", "-"], input=block, capture_output=True, text=True
            )
            if result.returncode != 0:
                raise AssertionError(f"script[{index}] 不是合法 JS：{result.stderr.strip()[:200]}")
            continue
        for number, line in enumerate(block.splitlines(), 1):
            stripped = re.sub(r"\\.", "", line)
            stripped = re.sub(r"//.*", "", stripped)
            if stripped.count('"') % 2:
                raise AssertionError(f"script[{index}] L{number} 双引号未闭合：{line.strip()[:80]}")
    _assert_no_undefined_calls(html)


# Names the injected scripts may use without declaring them: browser builtins,
# plus what viewer.html itself defines above them.
_JS_GLOBALS = frozenset({
    "fetch", "setInterval", "clearInterval", "setTimeout", "clearTimeout",
    "parseInt", "parseFloat", "String", "Number", "Boolean", "Array", "Object",
    "JSON", "Math", "Date", "Error", "Set", "Map", "Promise", "RegExp",
    "document", "window", "location", "console", "isNaN", "alert",
    "encodeURIComponent", "decodeURIComponent", "FileReader",
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


def _assert_no_undefined_calls(html: str) -> None:
    """Catch a call to a name the injected script never defines.

    `node --check` only parses; it cannot know that `attachLive()` refers to
    nothing. That is exactly how a silently non-matching edit shipped a page
    whose composer threw "Can't find variable" on the first click while every
    test still passed — the failure is at first call, not at parse.

    Only the scripts this module appends are checked. They come after the
    viewer's own closing tag, and they are the only ones written here; holding
    viewer.html to this rule would report its helpers as undefined and drown
    the real signal.
    """

    injected = html.split("</html>", 1)[-1]
    for index, block in enumerate(_script_blocks(injected)):
        source = _strip_js_literals(block)
        declared = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", source))
        declared |= set(re.findall(r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)", source))
        declared |= set(re.findall(r"([A-Za-z_$][\w$]*)\s*=\s*function", source))
        called = set(re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", source))
        missing = sorted(called - declared - _JS_GLOBALS - _JS_KEYWORDS)
        if missing:
            raise AssertionError(f"注入脚本[{index}] 调用了未定义的名字：{missing}")


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
