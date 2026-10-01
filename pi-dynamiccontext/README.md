# pi-dynamiccontext

DynamicContext 运行时的完整移植：给 append-only 会话加上**声明式生命周期**这一层，配置直接读项目的 `runtime.yaml`。

会话历史一字节不动；所有变化都是可见性变化。两层投影共同承载这一语义：

- **context hook** — 每次 LLM 调用前的实时 π(history, life_cycle)：模型看到的恰好是当前轮应该在场的槽位
- **context_edit** — 同一事实在每轮结算时落账的持久记录：transcript 界面与不带扩展的重载都保持诚实

## 安装

```bash
# 本地试用（指向包目录或入口文件均可）
pi -e /path/to/DynamicContext/pi-dynamiccontext

# 装入全局（~/.pi/agent/extensions/）
pi install /path/to/DynamicContext/pi-dynamiccontext
```

配置：`--dc-config <path>` 或环境变量 `DC_CONFIG` 指定 runtime.yaml；缺省依次找 `./config/standard/runtime.yaml`、内置默认。

## life_cycle

每个元素声明 `[start, end]` 闭区间；未声明的元素默认 `[born, null]`（出生即在场、永不退役）。

| 边界写法 | 语义 |
|---|---|
| `born` | 槽位出生轮 |
| `born+n` | 出生轮 + n |
| 数字 | 绝对轮次 |
| `null` / `permanent` | 无结束 |
| `until_cancelled` / `until_goal_end` | 保持开放，由事件关闭 |

内置默认补充了 runtime 没有的元素：`toolResult: [born, born+5]`、`recall: [born, born]`。

**轮的口径**：一轮 = 一次完整的用户交互。工具循环内部不推进轮次——同一次请求里出生的工具结果共享同一个 bornTurn。

**多段区间**：槽位的可见区间是段列表；reopen 追加新段而不重算内容。规则在槽位出生时快照。

## 补丁

内容补丁（用户输入里直接写，可内联）：

```
/goal 翻译本文 remain 2      # 落地一次，活 2 轮
/system 新提示词 refresh none # 结束当前值，新值永久在场
/goal 每日提醒 roll 5         # 覆盖期内每轮重新落地一格
对话正文 /goal 翻译 remain 3  # 内联：补丁剥离，正文照常进模型
```

规则补丁（改元素的生命周期声明）：

| 命令 | 语义 |
|---|---|
| `/dc-rule <el> <start> <end>` | forward：只管之后出生的槽位 |
| `/dc-rule <el> <start> <end> force_refresh` | 按各槽位出生轮用新规则重算结束轮 |
| `/dc-rule <el> <start> <end> reset_from_now` | 本轮结束全部存量，新规则只管新槽位 |
| `/dc-addrow <el> <start> <end>` | 声明新元素 |

`context.patch_end` 启用时：补丁元素最后一格关闭的下一轮，自动生成 `{element}_end` 结束语（模板来自配置，活一轮），并把该元素辖治的轮次就地折叠（`compact.fields` 内的元素退役——零模型调用的压缩）。

## recall

`recall.type: grep`：ASCII 按词、CJK 按 bigram 的词面重叠打分，top_k/min_score 可配，`search_fields` 声明可搜的证据字段。`recall.trigger` 决定何时触发：`always` / `never` / `manual` / `pattern`（正则来自配置）。命中注入 `recall` 槽位（遵守自己的 life_cycle，一般 `[born, born]` 只当轮可见）。

`dc_recall(query)` 工具始终可用，供模型主动捞取——含已退役槽位，带 `[retired]` 标注。`/dc-recall <query>` 是人工预览。

## compact

周期（`interval_turns`）/ 超量（`overload_threshold_bytes`，按线上字节数计）/ 手动（`/dc-compact`）三触发。窗口 = 最早未压缩轮到 `当前轮 - keep_recent_turns`；摘要一次算出、永不再喂回模型；摘要是落在锚轮位置的槽位（`{endTurn}_summary`），带 `compact_range` 记录覆盖范围。retention 策略：`latest_only` / `keep_all` / `last_k` / `equidistant`（等距稀疏化，远处历史变薄而不是消失）。

pi 自带的 threshold/overflow 压缩触发时，摘要内容改由本扩展的压缩机生成（`session_before_compact` 接管），pi 的压缩条目原生落账。

## pin

`/pin <内容>` 把一条永久便签贴到账本（`pin_user_pNN` 槽位）；`/cancelpin pNN` 从此轮起结束它——过去轮次"便签曾在场"的事实不改写。

`/dc-keep [entryId]` / `/dc-unkeep <entryId|all>` 是另一回事：保护某条已有槽位不被退役。

## 命令一览

| 命令 | 作用 |
|---|---|
| `/dc-status` | 轮次、规则表、补丁队列、每个槽位的区间与在场状态 |
| `/dc-rule` / `/dc-addrow` | 规则补丁（上表） |
| `/pin` / `/cancelpin` | 便签 |
| `/dc-keep` / `/dc-unkeep` | 退役保护 |
| `/dc-compact` | 本轮结算时手动压缩 |
| `/dc-recall <query>` | 人工回忆预览 |

## 持久化

每次结算写一份快照（`dynamiccontext` custom entry，schema v2）：规则表、全部槽位区间、keep 集合、补丁队列、轮次。重启后从最新快照恢复；旧 schema 快照不迁移、直接放弃。

## 边界

- 与其它重写消息列表的上下文插件互斥（如 billion-context-pi）：两者都在 `context` 钩子里改写消息、都接管压缩，同时装会互相覆盖。只保留一个。
- 恢复（reopen）对纯文本无损；图片/工具调用类 content 恢复为文本块。
- `cycle` / `labelled` 规则未移植：它们回答自评测数据集的逐轮标注，会话里没有数据源。
- 畸形补丁行（/ 开头但不合补丁语法）不报错、原样透传给模型——pi 的其它斜杠命令也是这个形态，无法可靠区分。
