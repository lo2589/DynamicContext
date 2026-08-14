# Simple Chat Runtime

一个 YAML 驱动的多轮对话 runtime。核心主张：上下文不是一段拼起来的文本，是两个正交的维度——

- **纵向**：一次发言拆成哪些槽位（`user` / `think` / `assistant` / 模型自己开的标签…）
- **横向**：每个槽位在哪些轮次可见，由一条声明决定，不是由代码里散落的判断决定

对模型而言，看到的仍然是普通的 `user`/`assistant` 消息列表；兼容性在渲染层解决，表达力在账本层获得。

## 启动

```bash
cd DynamicContext
python3 main.py --config config/standard/runtime.yaml
```

没有现成 yaml 时，给一个输出目录，加载器会把默认 yaml 复制进去再从副本启动：

```bash
python3 main.py --output-dir ./task/new_experiment
```

公开仓库只包含脱敏样例 `config/standard/` 和 `task/standard/`。其他
`config/`、`task/` 内容以及 provider 凭据默认被 Git 忽略，仅保留在本机。

`--config` 和 `--output-dir` 二选一，都不给直接报错。终端还能补充：

```text
--input-type     输入类型
--input-path     输入文件
--dataset-path   数据输出目录
```

优先级是 `终端输入 > YAML > 默认值`；终端输入会先写回实验 yaml，再从这份 yaml 重新加载——运行期间只有一份生效配置，不是内存里叠了好几层。

## 装配顺序

`main.py` 只保留这五行，可见即真实：

```python
cfg = load_cfg()
tables = manager["tables.initialize"](cfg)
input_data = dataset["input.build"](cfg, tables=tables)
runtime = manager["runtime.build"](cfg, tables=tables, input_data=input_data)
manager["runtime.run"](runtime)
```

每一轮内部：

```text
生命周期规则逐个结算（谁该退场、谁该回场）
→ 写入本轮 user
→ context 由声明投影出来 → 调用 provider
→ 解析回答，原始响应先落盘（raw.jsonl），再切成 think / assistant / 模型自定义标签
→ validate → 四表提交
```

用户在生成期间打断时，`chat.normal` 返回已经生成的部分，runtime 照常解析并保存；provider 异常不会提交这一轮。

## 槽位与区间

一条记录是 `(轮, 槽位) → (内容, 区间列表)`：

```json
"range": [[[21, 21], 1.0, "none"], [[23, null], 1.0, "none"]]
```

每段是 `[[起, 止], 密度, 压缩器]`，**闭区间**：上例读作"第 21 轮出场，第 22 轮缺席，第 23 轮起再次出场"。中间的缺口不是删除的痕迹——账本里那条内容一个字没动，区间列表只是记录了它曾经、以及此刻是否出场。

写入的通道互不相识，各自命名自己的记录：输入回声写 `user`；输出解析写 `think`、`assistant`、`pin_*`；压缩写 `NN_summary`；回捞写 `recall`；运行时补丁写配置中未声明的新元素。没有中央调度按名字分派。

## 生命周期：区间的端点是函数，不是常数

`life_cycle` 的一条声明形如 `[起, 止]`，`止` 是一个注册过的函数名：

```yaml
life_cycle:
  system:    [1, null]
  user:      [born, cycle]
  think:     [born, born]        # 想完就扔，不进下一轮
  assistant: [born, cycle]
```

当前注册表（`code/lifecycle/rules.py`）：

| 名字 | 答案 | 何时可知 |
|---|---|---|
| `born` | 生在哪轮就哪轮结束 | 出生时 |
| `born+n` | 出生轮 + n（`n` 是参数，`born+3` 会拆成 `born_add(n=3)`，不会把参数塞进名字里） | 出生时 |
| `permanent` | 不结束（写 `null` 等价于这个） | 出生时 |
| `until_cancelled` / `until_goal_end` | 等一个事件 | 事件到达时 |
| `cycle` | 输入指回这一轮时缺席 | 每轮 |
| `labelled` | 输入打了标的按 `born` 结束，没打的按 `permanent` 不结束 | 每轮 |

一条规则属于"出生时就能算完"还是"每轮都要重新问"，写在函数自己身上（`@each_turn` 装饰器），不是靠反射猜函数签名。加一条新规则就是加一个函数 + 一次注册，不改边界解析、不改失效队列、不改流的格式，也不改其余任何一条规则。

## 注册表

八个目录各自维护一份注册表，yaml 里写名字，运行时查表拿函数：

```text
lifecycle   born / born_add / permanent / cycle / labelled / until_cancelled / until_goal_end
compact     summary / sum / goal_collapse / retain.last_k / retain.keep_all / retain.equidistant / retain.latest_only / add_constant
recall      grep / trigger.always / trigger.manual / trigger.never / trigger.pattern / none
patch       add_row / cell / create / forward / force_refresh / reset_from_now / content.for_turn / content.pending_after / input.extract / input.parse / row / yaml.write
dataset     input.type.* / input.interface.* / storage.local / config.load / history.* / context.resume …
provider    client.deepseek / client.glm / client.minimax / client.ollama / client.dry-run / cfg.* / chat.normal / chat.no_stream / answer.parse.normal / answer.parse.tagged / tools.none / search.none
saver       history.save / state.save / context.save / life_cycle.save / patch.append / raw.append / print.tables / print.none
manager     tables.initialize / runtime.build / runtime.run / lifecycle.fixed / context.select.none
```

`chat.no_stream` 是给流式响应不可靠的 provider 用的——它调 provider 的非流式接口，一次性拿完整 message 再解析，避免流式分片在推理/回答边界上出错。

## Provider 配置

每个 `config/config-provider/*.json` 是一份独立的 `(厂商, 模型, base_url, key, timeout)` 配置，yaml 里 `provider.config` 直接按文件名引用——一个模型一个文件，不是一个文件塞多个模型（`ProviderConfig.model` 是标量字符串，这是运行时的数据结构决定的，不是约定）。

```bash
python3 -m code.provider init      # 交互式创建一份新配置
python3 -m code.provider show      # 查看已保存的配置（不显示 key 原文）
python3 -m code.provider dry-run --turn-id 5     # 固定返回 <think>5</think> id：5，不用真的调用
python3 -m code.provider chat "只回答 OK" --turn-id 1   # 用某份配置发一次真实请求
```

支持的厂商：`dry-run` / `glm` / `deepseek` / `minimax` / `ollama`。

## 压缩、回捞、钉住

三者签名一致：压缩 `流 × 字段集 × 压缩器 → 流`，回捞 `流 × 查询 → 流`，钉住 `流 × 内容 → 流`。摘要是流中一条普通记录，"压缩摘要"就是压缩作用在一条恰好是摘要的记录上，不是另一套代码路径。

`pin`（`until_cancelled`）是唯一让内容不随距离衰减的机制——被钉住的东西声明上永远在场，不需要被重新提起才能留下来。

## 可重算

`state = π(流, 声明)`，`context = render(state)`——投影不写流，因此状态和上下文完全由流与声明决定：状态文件可以删除后重算，相同输入必得相同上下文，改声明不用回溯改数据。代价是每轮重放全流，单轮开销随轮数线性增长。

## 自测

```bash
python3 run_self_tests.py
```

在一个解释器里依次导入每个模块并跑它的 `_self_test`，避免 `python -m` 重复导入触发注册表的重名检查。
