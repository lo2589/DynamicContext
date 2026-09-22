# pi-dynamiccontext

DynamicContext 的 Pi 扩展包：给 pi 的 append-only 会话加上**声明式生命周期规则**这一层。

pi 原生提供 raw history（不改）与派生 context（context_edit 省略/替换），但每一次可见性变化都得手工或扩展逐条 append。本扩展把中间那层补上：每条 entry 出生时登记为槽位并快照一条生命周期规则，每轮 `turn_end` 结算一次，diff 以 pi 原生 `context_edit` 落账——**raw history 一字节不动，退役只是可见性变化，不是删除**。

## 安装

```bash
# 本地试用（当前仓库内）
pi -e /path/to/DynamicContext/pi-dynamiccontext

# 装入全局
pi install /path/to/DynamicContext/pi-dynamiccontext
```

## 规则

| 规则 | 语义 |
|---|---|
| `born` | 只在出生轮可见 |
| `born+n` | 出生后再保留 n 轮 |
| `permanent` | 永不自动退役（默认：user / assistant / custom_message） |

默认规则：`toolResult: born+5`，其余 permanent。规则在槽位**出生时快照**——`/dc-rule` 改的是未来槽位的默认（forward 语义），不动存量。

## 命令

| 命令 | 作用 |
|---|---|
| `/dc-status` | 槽位总数、在场/退役计数、pin 列表、当前规则表 |
| `/dc-rule <element> <rule>` | 改某类槽位未来出生的默认规则 |
| `/pin [entryId]` | 钉住一条 entry，永不退役（默认最近一条消息） |
| `/unpin <entryId\|all>` | 取消钉住，下次结算生效 |

## 工具（模型可调用）

- `dc_recall(query)`：搜全部账本**含已退役槽位**——"退役 ≠ 删除"，说过的话永远可捞回。

## v0 边界

- 恢复（reopen）通过 `replacement: 原文` 实现，对纯文本无损；图片/工具调用类 content 恢复为文本块。
- 与 pi 自带的 compaction 共存但未做联动：compaction 边界之前的 entry 由 pi 汇总，本扩展的编辑应用在投影层之后。
- 规则修改是 forward-only；`force_refresh`（按出生轮次重算存量）留待后续版本。
