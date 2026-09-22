# DynamicContext

一个由 YAML 驱动、把对话上下文保存为可审计槽位账本的本地多轮对话 runtime。

普通聊天系统通常把上下文当成不断增长的消息列表；这个项目把它拆成两个正交维度：

- 一轮由哪些槽位组成，例如 `user`、`think`、`assistant`、`recall`、`pin_*` 和模型自定义标签。
- 每个槽位在哪些轮次进入 context，由生命周期声明决定，而不是散落在代码里的特殊判断。

Provider 最终收到的仍是标准 `system` / `user` / `assistant` 消息。额外结构只存在于本地账本和投影层，因此可以检查“历史写过什么”“当前保留什么”以及“这一轮实际发送了什么”。

> 当前状态：experimental。核心 runtime、真实模型调用、生命周期、压缩、回捞、pin、运行时 patch 和本地可视化均可使用；`tools` 与 `search` 目前只有 `none` 实现。所有 HTTP 服务仅绑定 `127.0.0.1`，但写接口尚未加入 Origin/会话令牌校验，不要把端口转发或反向代理到局域网、公网。

## 环境要求

- Python 3.11（当前开发和自测版本；代码至少需要 Python 3.10）
- `pip`
- 一个真实模型服务：Ollama，或 GLM / DeepSeek / MiniMax 的兼容 API

安装：

```bash
git clone <repository-url>
cd DynamicContext

python3 -m venv .venv
source .venv/bin/activate       # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 最快启动

没有现成配置时，启动本地设置面板：

```bash
python3 start.py
```

浏览器会打开 `http://127.0.0.1:8775/`。选择 provider、model、任务名和输入方式后，面板会在本机创建：

- `config/config-provider/<name>.json`：provider、model、base URL、API key 和 timeout。
- `task/<name>/runtime.yaml`：这个会话唯一生效的运行配置。
- `task/<name>/` 下的固定表：历史、状态、context、生命周期、patch 和原始响应。

Provider 配置与非标准任务默认被 Git 忽略。API key 只写入本机 provider 配置，页面和 `show` 命令不会回显其原文。

已有 YAML 时可直接运行：

```bash
python3 main.py --config task/<name>/runtime.yaml
```

同时托管多个任务：

```bash
python3 serve_all.py
python3 serve_all.py --task first --task second --port 9000
```

## 命令行配置 Provider

标准 YAML `config/standard/runtime.yaml` 引用本机的 `config/config-provider/provider.json`。首次使用前创建它：

```bash
# Ollama；默认会执行 ollama pull
python3 -m code.provider init --vendor ollama --model qwen3:8b

# 云端 provider；key 会写入权限为 0600 的本地配置文件
python3 -m code.provider init --vendor glm --model GLM-4.5-Air
python3 -m code.provider init --vendor deepseek --api-key "$DEEPSEEK_API_KEY"
python3 -m code.provider init --vendor minimax
```

然后可以启动标准任务：

```bash
python3 main.py --config config/standard/runtime.yaml
```

检查配置或直接发一次真实请求：

```bash
python3 -m code.provider show
python3 -m code.provider chat "只回答 OK" --turn-id 1
```

运行时没有可由 YAML 选择的 dry-run/fake provider。自测使用的确定性 stub 只在测试函数内部通过依赖注入创建，生产配置无法访问。

## 配置模型

一次运行只有一份生效 YAML。入口装配顺序保持显式：

```python
cfg = load_cfg()
tables = manager["tables.initialize"](cfg)
input_data = dataset["input.build"](cfg, tables=tables)
runtime = manager["runtime.build"](cfg, tables=tables, input_data=input_data)
manager["runtime.run"](runtime)
```

主要配置区：

| 区域 | 作用 |
|---|---|
| `provider` | 引用本机 provider JSON |
| `chat` / `answer` | 调用方式、输出解析和槽位角色 |
| `input_data` | 终端、GUI 或数据集输入 |
| `life_cycle` | 各类槽位的出生与结束规则 |
| `compact` | 压缩字段、阈值、周期和保留窗口 |
| `recall` | 回捞实现与触发规则 |
| `dataset` | 任务目录和固定表文件名 |
| `runtime.viewer` | 本地网页、端口、刷新间隔 |
| `context` | context 投影、必需槽位、角色和 patch |

命令行的 `--input-type`、`--input-path`、`--dataset-path` 会覆盖 YAML，并先写回任务 YAML 再重新加载。运行期间不会叠加第二份隐式配置。

## 账本：槽位 × 轮次

历史不记"消息列表"，记槽位。一条槽位记录由 `(turn, element)` 唯一标识，`element` 是槽位类型（`user`、`think`、`assistant`、`recall`、`pin_*`、模型自定义标签……）：

```json
{
  "content": "example",
  "range": [[[21, 21], 1.0, "none"], [[23, null], 1.0, "none"]]
}
```

`range` 是闭区间列表，每项为 `[[起始轮, 结束轮], 权重, 模式]`，`结束轮: null` 表示开放。上面这个例子表示该槽位第 21 轮可见、第 22 轮缺席、第 23 轮起重新可见。**内容落账后不再改动；可见性的全部变化都写在区间上**，包括"暂时消失"和"重新出现"——在消息列表模型里这两件事只能用删除和重新插入来伪造，而伪造会弄丢"它一直在那里，只是那一轮没上场"这个事实。

`history.jsonl` 只追加。state 与 context 都是派生物：

```text
state   = π(history, life_cycle)     # 每个槽位当前在不在场
context = render(state)              # 投影成本轮实际发给 provider 的消息
```

两者随时可以从账本和声明重算：改生命周期声明不需要回写历史内容，删掉投影文件重建即可。

## 生命周期是函数，不是标记

`life_cycle.json` 给每种槽位声明一条结束规则。规则不是枚举值，而是注册表里的函数，每个函数只回答一个问题：**这个区间在哪一轮结束**。按回答这个问题需要多少信息，规则分三类：

| 类别 | 规则 | 结算方式 |
|---|---|---|
| 出生即可定 | `born`（出生轮即终）、`born+n`（再留 n 轮）、`permanent`（开放） | 写入时折成一个结束轮，此后不再过问 |
| 依赖当前轮 | `cycle`（输入重新指向该轮时退场）、`labelled`（按数据集标记决定临时或永久） | 每轮重新问一遍；函数必须带 `@each_turn` 标记才会被逐轮调用 |
| 等待事件 | `until_cancelled`（pin 用）、`until_goal_end` | 出生时区间保持开放，事件发生时由 `end_cell` 关闭 |

规则名必须在注册表里存在，拼错即报错并列出全部可用名。`each_turn` 标记与函数签名的一致性由自测强制——一个需要当前轮却忘了声明的规则，会静默退化成只在出生时结算一次，这类故障被挡在测试里。

每轮执行顺序：

```text
结算生命周期和运行时 patch
→ 写入本轮 user / recall 等输入槽位
→ 投影本轮实际 context
→ 调用真实 provider
→ 保存 raw response 并解析 think / assistant / tags / pins
→ validate
→ 原子提交 history、state、context 和 life_cycle
→ 按配置检查压缩
```

Provider 失败时本轮不提交。用户中断流式生成时，已经生成的部分会被解析并作为一次完整事务提交。

## 运行时 patch：改规则与改内容，都不动历史

对话进行中可以在输入行直接打内容 patch（`/goal 翻译文本 remain 5`），也可以改某类槽位的生命周期规则。全部记录进只追加的 `patches.jsonl`。

内容 patch 三种模式：

| 模式 | 语义 |
|---|---|
| `remain n` | 落一个从本轮起持续 n 轮的槽位，内容固定 |
| `roll n` | 覆盖区间内每一轮都重新落一个一轮槽位，值跟随最新一次出现 |
| `refresh` | 终结该元素当前活跃的内容；新内容不再自行过期，直到被任何后续 patch 替换 |

规则 patch 三种模式，回答"存量槽位怎么办"：

| 模式 | 语义 |
|---|---|
| `forward` | 旧槽位不动，新规则只管之后出生的槽位 |
| `force_refresh` | 按各旧槽位自己的出生轮次，用新规则重算结束轮 |
| `reset_from_now` | 本轮结束全部旧槽位，新规则只管之后 |

另有 `cell` patch 只重算指定的单个旧槽位。

## 压缩、回捞、pin

- **压缩是槽位上的轴，不是外挂步骤**：compact 配置声明阈值、周期与保留窗口；到点把指定槽位压成 summary，summary 自己是账本里的一个槽位，有自己的区间与生命周期。
- **回捞搜的是全部账本，包括已退役槽位**：可见性退役 ≠ 真值失效——"第 12 轮说过的话"永远为真，只是不再每轮上场。grep 实现按词法 token 检索，触发器分 `always` / `never` / `manual` / `pattern` 四档。
- **pin 就是一条 `until_cancelled` 规则**：钉住的内容保留到显式取消，没有特权代码路径。

## 固定表

每个任务目录通常包含：

| 文件 | 内容 |
|---|---|
| `history.jsonl` | 只追加的槽位账本 |
| `state_latest.json` | 当前槽位可见性投影 |
| `context_latest.json` | 最近一次提交后的 OpenAI 形状 context |
| `life_cycle.json` | 生命周期声明 |
| `patches.jsonl` | 运行时 patch 记录 |
| `raw_history.jsonl` | provider 原始输出 |
| `snapshots/` | 可选的周期快照 |

仓库只跟踪合成的 `task/standard/` 样例，不包含真实用户数据、凭据或 benchmark 输出。

## 本地网页

将任务 YAML 中的 `input_data.interface` 设为 `gui`，并启用 `runtime.viewer.enabled`，即可在本地网页中：

- 发送消息并查看真实流式输出。
- 同步查看本轮 context、聊天灰态、slot × turn 矩阵和 token 估算。
- 切换已保存模型配置。
- 编辑生命周期规则，执行 pin、压缩、隐藏、恢复和删除操作。
- 在多个本地任务间切换。

页面和管理服务只监听 loopback。它们不是为多用户、远程部署或不受信任网页环境设计的；在 Origin/令牌保护完成前，不要通过 SSH 转发、容器端口映射或反向代理暴露。

## 自测

```bash
python3 run_self_tests.py
```

自测在一个解释器中依次导入模块并运行各模块的 `_self_test`。需要绑定 loopback 端口的 viewer/launcher 测试必须允许本机 socket。

当前完整自测结果：25 passed，0 failed，1 个模块没有自测入口。真实模型是否可用仍取决于本机 provider 服务与配置。

## 项目边界

- 已实现：真实 provider、流式与非流式调用、固定表事务、生命周期、运行时 patch、summary 压缩、grep recall、pin、恢复、本地 viewer、单任务与多任务托管。
- 尚未实现：非 `none` 的 tools/search provider 集成、远程/多用户服务、HTTP 写接口的 Origin/令牌防护。
- 性能取舍：每轮会重放账本以重算投影，单轮开销随历史长度线性增长。

## 发布与贡献

提交前至少运行：

```bash
python3 run_self_tests.py
git diff --check
```

请勿提交 `task/` 中的真实会话、`config/config-provider/`、API key、模型输出或浏览器测试截图。若改动生命周期、投影或 viewer 时序，应同时增加对应模块自测，并用真实 provider 验证生成中与落账后的状态一致。

本项目尚未选择开源许可证。在根目录加入明确的 `LICENSE` 之前，公开可见不代表获得复制、修改或分发许可。
