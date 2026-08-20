// Injected into viewer.html by code/saver/live_view.py.
// window.__live carries the per-run values the page needs.
(function () {
  var last = null
  var label = window.__live.label
  var interval = window.__live.interval

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

// The composer and everything attached to it only exist when this page was
// served with somewhere to send input. Polling above runs either way.
;(function () {
  if (!window.__live.writable) return

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
    fetch("interrupt", { method: "POST" })
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

  // One node per bubble, created once and then only having its text updated.
  // Rebuilding innerHTML on every poll — 750ms while generating — destroyed
  // and recreated these nodes more than once a second, which reflowed the
  // block and restarted any CSS animation from zero. That is what read as
  // flashing: not a pulse, a strobe.
  var slots = {}

  function slotNode(key, className, tagText) {
    var node = slots[key]
    if (node === undefined) {
      node = document.createElement("div")
      node.className = "b " + className
      if (tagText) {
        var tag = document.createElement("span")
        tag.className = "tag"
        tag.textContent = tagText
        node.appendChild(tag)
      }
      node.appendChild(document.createTextNode(""))
      slots[key] = node
    }
    if (node.parentNode !== live) live.appendChild(node)
    return node
  }

  function setSlot(key, className, tagText, text) {
    if (!text) {
      var existing = slots[key]
      if (existing && existing.parentNode) existing.parentNode.removeChild(existing)
      return
    }
    var node = slotNode(key, className, tagText)
    var body = node.lastChild
    if (body.nodeValue !== text) body.nodeValue = text
  }

  function renderLive(text) {
    if (!text && !pendingUser) {
      live.innerHTML = ""
      slots = {}
      return
    }
    attachLive()
    var think = "", answer = text || ""
    var open = answer.indexOf("<think>")
    if (open !== -1) {
      var close = answer.indexOf("</think>")
      if (close === -1) { think = answer.slice(open + 7); answer = "" }
      else { think = answer.slice(open + 7, close); answer = answer.slice(close + 8) }
    }
    setSlot("user", "u", "", pendingUser)
    setSlot("think", "t", "think · generating", think.trim().slice(-400))
    setSlot("answer", "a", "assistant · generating", answer.trim())
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
    fetch("send", {
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
          fetch("streaming", { cache: "no-store" })
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
            return fetch("last-error", { cache: "no-store" })
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
    return fetch("sessions", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null })
      .then(function (d) {
        if (!d) return
        if (d.launcher) window.__launcherPort = d.launcher
        if (d.hub) window.__hubPort = d.hub
        // Hosted: every session lives in one process behind /s/<task>/, so a
        // switch is a path. Standalone: each is its own process on its own
        // port, so it is a port. The option value carries whichever applies.
        window.__hosted = !!d.hosted
        var here = d.here
        var html = ""
        var seen = false
        ;(d.sessions || []).forEach(function (s) {
          var mine = s.task === here
          if (mine) seen = true
          var target = d.hosted ? "/s/" + s.task + "/" : s.port
          html += '<option value="' + target + '"' + (mine ? " selected" : "") + ">" +
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
    if (!sessionSel.value) return
    location.href = window.__hosted
      ? sessionSel.value
      : "http://127.0.0.1:" + sessionSel.value + "/"
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
    return fetch("tokens", { cache: "no-store" })
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
    fetch("new-run", { method: "POST" })
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
  // Published deliberately: the per-turn controls live in their own IIFE and
  // cannot see this scope. Referencing a name across that boundary throws a
  // ReferenceError at the first click — it is not merely undefined — so
  // anything shared between the blocks goes on window, the same way
  // __liveTick does.
  window.__refreshTokens = refreshTokens

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
    fetch("settings", { cache: "no-store" })
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
    fetch("settings", {
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
    return fetch("models", { cache: "no-store" })
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
    fetch("model", {
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

// ---- per-turn visibility: hide / restore / kill ----
// The viewer renders the transcript as "turn N" markers followed by that
// turn's bubbles, and rewrites #chat wholesale on every draw. So the controls
// are re-injected after each draw rather than placed once — an observer, not
// a one-time pass — which also means viewer.html itself needs no change.
;(function () {
  if (!window.__live.writable) return

  var chat = document.getElementById("chat")
  if (!chat) return

  function turnOf(marker) {
    var m = /(\d+)/.exec(marker.textContent || "")
    return m ? parseInt(m[1], 10) : null
  }

  function act(action, turn, marker) {
    var ops = marker.querySelector(".turn-ops")
    function enable(state) {
      if (ops) Array.prototype.forEach.call(ops.querySelectorAll("button"), function (b) { b.disabled = state })
    }
    // Re-enabled when the request settles. Leaving them disabled and waiting
    // for the next redraw to replace them means a turn whose transcript did
    // not change keeps a row of dead buttons.
    enable(true)
    fetch("turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: action, turn: turn }),
    })
      .then(function (r) { return r.json().then(function (d) { if (!r.ok) throw new Error(d.error); return d }) })
      .then(function (d) {
        // A refusal is information, not a failure: think that is declared
        // [born, born] is finished by its own rule, and a killed record does
        // not come back from a button.
        var refused = d.refused ? Object.keys(d.refused) : []
        if (refused.length) {
          var first = d.refused[refused[0]]
          say(marker, refused.join("/") + "：" + first)
        }
        return window.__liveTick()
      })
      .then(function () { if (window.__refreshTokens) window.__refreshTokens() })
      .catch(function (e) { say(marker, "失败：" + e.message) })
      .then(function () { enable(false) })
  }

  function say(marker, text) {
    var note = marker.querySelector(".said")
    if (!note) return
    note.textContent = text
    setTimeout(function () { if (note) note.textContent = "" }, 6000)
  }

  function decorate() {
    Array.prototype.forEach.call(chat.querySelectorAll(".turnmark"), function (marker) {
      if (marker.querySelector(".turn-ops")) return
      var turn = turnOf(marker)
      if (turn === null || turn === 0) return  // turn 0 is the system prompt

      var ops = document.createElement("span")
      ops.className = "turn-ops"
      ops.innerHTML =
        '<button data-a="hide" title="从下一轮起不再进入上下文；内容保留在账本里，可恢复">不可见</button>' +
        '<button data-a="show" title="重新进入上下文（声明已到期或已删除的槽位不会恢复）">恢复</button>' +
        '<button data-a="delete" class="kill" title="标记为已删除并记在当前轮；账本保留记录，但按钮无法撤销">删除</button>' +
        '<span class="said"></span>'
      Array.prototype.forEach.call(ops.querySelectorAll("button"), function (button) {
        button.onclick = function (event) {
          event.stopPropagation()
          var action = button.getAttribute("data-a")
          if (action === "delete" &&
              !confirm("删除第 " + turn + " 轮？\n\n账本会保留这条记录并标记为已删除，但恢复只能手动改 history.jsonl。")) {
            return
          }
          act(action, turn, marker)
        }
      })
      marker.appendChild(ops)
    })
  }

  decorate()
  new MutationObserver(decorate).observe(chat, { childList: true })
})()

// ---- reasoning opens on click ----
// Delegated on #chat because render() replaces its children wholesale; a
// listener bound to each bubble would be gone on the next draw.
;(function () {
  if (!window.__live.writable) return
  var chat = document.getElementById("chat")
  if (!chat) return
  chat.addEventListener("click", function (event) {
    var bubble = event.target.closest ? event.target.closest(".b.t") : null
    if (bubble && chat.contains(bubble)) bubble.classList.toggle("open")
  })
})()
