// 一件事：把模型写的 Markdown 变成能看的 HTML。
//
// 模型答一段带表格的内容，账本里存的是原文（那一堆竖线），viewer 原来只做
// HTML 转义再塞进气泡 —— 转义之后换行被 HTML 折叠，整张表挤成一行，谁也读
// 不了。渲染是前端的事，所以放前端；渲染是一件事，所以放一个文件、一个入口
// 函数 mdToHtml(text) -> html，不散在各处的字符串拼接里。
//
// 只认模型真会写的那几种块：围栏代码、表格、标题、分隔线、引用、有序/无序
// 列表、段落；行内认 code、粗体、斜体、删除线、http 链接。不做完整
// CommonMark —— 那是另一个项目，装进来只会多一堆没人读的分支。
//
// 输出永远先转义再拼标签：模型的输出是数据，不是模板。
(function (root) {
  var TICK = String.fromCharCode(96)          // 反引号，不写进正则里
  var FENCE = TICK + TICK + TICK
  var TILDE = "~~~"
  var MARK = String.fromCharCode(1)           // 占位符，正文里不可能出现

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
  }

  // 行内代码要在转义和加粗之前抠出来：代码里的星号是代码，不是粗体。
  function pullCode(text, codes) {
    var out = ""
    var i = 0
    while (i < text.length) {
      var open = text.indexOf(TICK, i)
      if (open < 0) { out += text.slice(i); break }
      var close = text.indexOf(TICK, open + 1)
      if (close < 0) { out += text.slice(i); break }
      out += text.slice(i, open) + MARK + codes.length + MARK
      codes.push(text.slice(open + 1, close))
      i = close + 1
    }
    return out
  }

  function inline(text) {
    var codes = []
    var s = pullCode(String(text), codes)
    s = escapeHtml(s)
    s = s.replace(/!?\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener">$1</a>')
    // 一条正则同时收 **粗** 和 __粗__。写成两条的话，第二条会以 __( 开头，
    // 而 live_view 那个「调用了未定义的名字」的检查看不懂正则字面量，会把它
    // 当成调用了一个叫 __ 的函数。规避比放宽检查划算。
    s = s.replace(/(\*\*|__)([^*_]+)\1/g, "<strong>$2</strong>")
    s = s.replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>")
    s = s.replace(new RegExp(MARK + "(\\d+)" + MARK, "g"), function (whole, n) {
      return "<code>" + escapeHtml(codes[Number(n)]) + "</code>"
    })
    return s
  }

  function fenceOf(line) {
    var s = line.replace(/^\s+/, "")
    if (s.indexOf(FENCE) === 0) return FENCE
    if (s.indexOf(TILDE) === 0) return TILDE
    return null
  }

  // ── 表格 ──────────────────────────────────────────────────────────
  function isTableRow(line) {
    if (!/^\s*\|/.test(line)) return false
    return line.indexOf("|", line.indexOf("|") + 1) > 0
  }

  function splitRow(line) {
    // 单元格里写 \| 时那根竖线是内容，不是分隔符，先换成占位符再切。
    var s = line.trim().replace(/\\\|/g, MARK)
    if (s.charAt(0) === "|") s = s.slice(1)
    if (s.charAt(s.length - 1) === "|") s = s.slice(0, -1)
    return s.split("|").map(function (cell) {
      return cell.split(MARK).join("|").trim()
    })
  }

  function isTableRule(line) {
    if (!isTableRow(line)) return false
    var cells = splitRow(line)
    if (!cells.length) return false
    return cells.every(function (cell) { return /^:?-+:?$/.test(cell) })
  }

  function alignOf(cell) {
    var left = cell.charAt(0) === ":"
    var right = cell.charAt(cell.length - 1) === ":"
    if (left && right) return "center"
    if (right) return "right"
    return ""
  }

  function cellHtml(tag, text, how) {
    var style = how ? ' style="text-align:' + how + '"' : ""
    return "<" + tag + style + ">" + inline(text) + "</" + tag + ">"
  }

  function tableHtml(head, align, rows) {
    var html = '<div class="md-table-wrap"><table><thead><tr>'
    for (var i = 0; i < head.length; i++) html += cellHtml("th", head[i], align[i])
    html += "</tr></thead><tbody>"
    for (var r = 0; r < rows.length; r++) {
      html += "<tr>"
      for (var c = 0; c < head.length; c++) {
        html += cellHtml("td", rows[r][c] === undefined ? "" : rows[r][c], align[c])
      }
      html += "</tr>"
    }
    return html + "</tbody></table></div>"
  }

  // ── 列表 ──────────────────────────────────────────────────────────
  var ITEM = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/

  function parseList(lines, from) {
    var first = ITEM.exec(lines[from])
    var indent = first[1].length
    var ordered = /\d/.test(first[2])
    var items = []
    var i = from
    while (i < lines.length) {
      var m = ITEM.exec(lines[i])
      if (!m) {
        // 缩进的续行接到上一条上；别的任何东西都结束这个列表。
        if (items.length && /^\s+\S/.test(lines[i]) && !fenceOf(lines[i])) {
          items[items.length - 1].text += " " + lines[i].trim()
          i++
          continue
        }
        break
      }
      if (m[1].length >= indent + 2) {
        var sub = parseList(lines, i)            // 更深的缩进 = 嵌在上一条里
        if (items.length) items[items.length - 1].sub += sub.html
        i = sub.next
        continue
      }
      if (m[1].length < indent) break             // 退回到外层列表
      items.push({ text: m[3], sub: "" })
      i++
    }
    var tag = ordered ? "ol" : "ul"
    var html = "<" + tag + ">"
    for (var k = 0; k < items.length; k++) {
      html += "<li>" + inline(items[k].text) + items[k].sub + "</li>"
    }
    return { html: html + "</" + tag + ">", next: i }
  }

  // ── 块级 ──────────────────────────────────────────────────────────
  function startsBlock(line, next) {
    if (/^\s*$/.test(line)) return true
    if (fenceOf(line)) return true
    if (/^#{1,6}\s+/.test(line)) return true
    if (/^\s*>/.test(line)) return true
    if (ITEM.test(line)) return true
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) return true
    if (isTableRow(line) && next !== undefined && isTableRule(next)) return true
    return false
  }

  function blocks(src) {
    var lines = String(src).replace(/\r\n?/g, "\n").split("\n")
    var out = []
    var i = 0
    while (i < lines.length) {
      var line = lines[i]

      if (/^\s*$/.test(line)) { i++; continue }

      var fence = fenceOf(line)
      if (fence) {
        var body = []
        i++
        while (i < lines.length && fenceOf(lines[i]) !== fence) { body.push(lines[i]); i++ }
        i++
        out.push('<pre class="md-pre"><code>' + escapeHtml(body.join("\n")) + "</code></pre>")
        continue
      }

      if (isTableRow(line) && i + 1 < lines.length && isTableRule(lines[i + 1])) {
        var head = splitRow(line)
        var align = splitRow(lines[i + 1]).map(alignOf)
        i += 2
        var rows = []
        while (i < lines.length && isTableRow(lines[i]) && !isTableRule(lines[i])) {
          rows.push(splitRow(lines[i]))
          i++
        }
        out.push(tableHtml(head, align, rows))
        continue
      }

      var heading = /^(#{1,6})\s+(.*)$/.exec(line)
      if (heading) {
        var level = heading[1].length
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">")
        i++
        continue
      }

      if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
        out.push('<hr class="md-hr">')
        i++
        continue
      }

      if (/^\s*>/.test(line)) {
        var quoted = []
        while (i < lines.length && /^\s*>/.test(lines[i])) {
          quoted.push(lines[i].replace(/^\s*>\s?/, ""))
          i++
        }
        out.push("<blockquote>" + blocks(quoted.join("\n")) + "</blockquote>")
        continue
      }

      if (ITEM.test(line)) {
        var list = parseList(lines, i)
        out.push(list.html)
        i = list.next
        continue
      }

      var para = []
      while (i < lines.length && !startsBlock(lines[i], lines[i + 1])) {
        para.push(lines[i])
        i++
      }
      out.push("<p>" + para.map(inline).join("<br>") + "</p>")
    }
    return out.join("")
  }

  // 整段就是一份 JSON 时按 JSON 排版：数据集喂进来的 user、工具返回、解析前
  // 的原始输出都长这样，压成一行读不了，缩进过的才读得了。
  function asJson(text) {
    var s = String(text).trim()
    var head = s.charAt(0)
    if (head !== "{" && head !== "[") return null
    var value = null
    try { value = JSON.parse(s) } catch (err) { return null }
    if (value === null || typeof value !== "object") return null
    return '<pre class="md-pre md-json"><code>'
      + escapeHtml(JSON.stringify(value, null, 2)) + "</code></pre>"
  }

  function mdToHtml(text) {
    if (text === null || text === undefined) return ""
    return asJson(text) || blocks(text)
  }

  root.mdToHtml = mdToHtml

  // ── 接进 viewer ───────────────────────────────────────────────────
  // 不改 viewer.html：它的 render() 是全局函数，包一层就够了。包 render 而不
  // 是 bubble()，因为对话气泡、压缩摘要、右边 context 三处都要渲染，包一处
  // 比改三处的字符串拼接稳。
  if (typeof document === "undefined" || typeof root.render !== "function") return

  var cache = {}
  var cached = 0

  function toHtml(text) {
    if (cache[text] === undefined) {
      if (cached > 400) { cache = {}; cached = 0 }   // 轮询一直在重画，别无限长
      cache[text] = mdToHtml(text)
      cached++
    }
    return cache[text]
  }

  function streaming(node) {
    // 正在生成的气泡不碰：live.js 靠改文本节点的 nodeValue 逐字更新它，换成
    // innerHTML 就又开始闪，而且半张表格本来也渲染不出东西。
    var walk = node
    while (walk) {
      if (walk.id === "live-provisional") return true
      walk = walk.parentNode
    }
    return false
  }

  function paintOne(node) {
    var body = node.lastChild
    if (!body || body.nodeType !== 3) return        // 已经是标签，或者 (empty)
    if (streaming(node)) return
    var text = body.nodeValue
    if (!text || !text.trim()) return
    var box = document.createElement("div")
    box.className = "md"
    box.innerHTML = toHtml(text)
    node.replaceChild(box, body)
    // 82% 是聊天的宽度，不是表格的宽度。
    if (box.getElementsByTagName("table").length) node.classList.add("has-table")
  }

  function paint() {
    var nodes = document.querySelectorAll("#chat .b, #chat .compact-block, #ctx .cmsg")
    for (var i = 0; i < nodes.length; i++) paintOne(nodes[i])
  }

  var inner = root.render
  root.render = function () {
    var out = inner.apply(this, arguments)
    paint()
    return out
  }

  // view.py 导出的静态页在这一行之前就已经画完了，而且再也不会重画一次
  // ——只包 render 的话那种页面永远等不到渲染。补画一遍，页面是空的就什么
  // 也不做。
  paint()
})(typeof window === "undefined" ? this : window)
