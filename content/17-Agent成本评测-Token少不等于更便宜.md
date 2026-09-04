# 第 17 讲：Agent 成本评测——Token 少，不一定更便宜

我原本以为，统计一次模型响应里的 `total_tokens`，就算完成了 Agent 成本评测。

真正把数据接进评测链路后，这个想法很快出了问题：一个用户请求可能先经过 Router，再进入 Extractor 或 Query Planner；Tool Use 又会把工具 Schema 放进 Prompt。只看某一次调用，既算不清整条任务花了多少 Token，也解释不了为什么某种架构更贵。

这次我最终要解决的，不是“拿到一个 Token 数字”，而是：

> 把 Agent 的模型消耗从单次 API 返回值，变成可以沿 Trace 回溯、按 Case 聚合、在相同任务上比较的评测指标。

## 1. 成本评测先回答三个问题

Token、费用和性能不是一回事。

| 指标 | 回答的问题 | 本轮状态 |
|---|---|---|
| Token | 整条任务消耗了多少模型输入和输出 | 已实测 |
| 费用 | 按模型价格、缓存规则实际花多少钱 | 未核算 |
| 延迟 | 用户等了多久、慢在哪个节点 | 只有端到端均值，未做专项 |

因此，本轮的准确结论是“Token 成本和调用次数已经可测”，不是“已经完成完整性能测试”，也不能把 Token 更少直接写成费用更低。

Anthropic 在 Agent 评测指南中把 `n_total_tokens`、`n_toolcalls` 和延迟列为 transcript 级跟踪指标，并强调 Agent 的一次 trial 包含整段调用和中间结果，而不只是最后一条回答。这与本项目最后采用的口径一致：[Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)。

## 2. Token 是怎么接进来的

最终链路只有四步：

```text
SDK response.usage
→ 每次 llm_response 写入 Trace
→ 按 trace_id 收集本条 Case 的全部 usage
→ 按模型、Case 和轮次聚合
```

### 第一步：保留调用级原始数据

每次真实模型返回后，读取三个字段：

```python
Usage(
    prompt_tokens=response.usage.prompt_tokens,
    completion_tokens=response.usage.completion_tokens,
    total_tokens=response.usage.total_tokens,
)
```

这里不自行估算 Token，也不从文本长度反推，直接使用服务端响应中的 usage。Stub 默认没有 usage，因为它没有调用真实模型；Stub 全绿只能证明确定性编排没有坏，不能替代真实成本数据。

### 第二步：写进 Trace，不改业务返回

项目的业务响应有字段集合契约。如果为了统计成本，临时向业务 JSON 增加 `usage`，就会把观测字段和产品协议混在一起，还可能让契约断言失去意义。

所以 usage 只记录在对应节点的 `llm_response` 事件中：

```json
{
  "trace_id": "t-...",
  "node": "router",
  "event": "llm_response",
  "payload": {
    "usage": {
      "prompt_tokens": 623,
      "completion_tokens": 13,
      "total_tokens": 636
    }
  }
}
```

业务返回保持不变，评测 Runner 再从同一个 `trace_id` 下收集全部 usage。

### 第三步：按 Case 聚合所有调用

一个 Case 可能有一个或多个 `llm_response`：

```text
固定 record 链路：Router → Extractor
固定 query 链路： Router → Query Planner
Tool Use 链路：   模型选择工具 → 代码执行
```

因此，Case 成本的计算是：

```text
Case 总 Token = Σ 当前 trace_id 下每次模型调用的 total_tokens
```

报告再继续计算：

```text
token/Case = 当前模型总 Token ÷ 执行 Case 数
token/call = 当前模型总 Token ÷ 实际模型调用数
```

这里的分母必须写清。按 Case 平均回答“完成一次用户任务大约消耗多少”，按调用平均回答“一次模型请求有多重”，两者不能混用。

## 3. 一条真实 Case 为什么必须算完整链路

以“硬拉100kg三组，然后划船做了四组”为例，两种架构都正确写入两条记录，但路径不同：

| 架构 | 模型调用 | Token/Case | 结果 |
|---|---:|---:|---|
| 固定链路 | 2 | 1,481 | 写入 2 条，PASS |
| Tool Use | 1 | 1,231 | 写入 2 条，PASS |

如果只拿最后一次模型响应比较，就会漏掉固定链路前面的 Router 调用。只有沿 Trace 聚合，才能看到完成同一个任务时，固定链路实际多消耗了约 250 Token。

这也是为什么 Agent 成本必须按任务统计，而不是按某个 Prompt 或某次 API 调用统计。

## 4. 过程中真正踩到的四个细节

### 4.1 零 Token 不一定是漏采

三条非法输入在进入模型前被 `bad_request` 拦截，三轮共形成 9 行零调用记录。它们的 Token 为 0 是正确行为：Trace 中没有 `llm_request`，证明防御层在模型前生效。

判断零 Token 是否异常，不能只看数字，要一起检查调用轨迹：

```text
预期不调用 + Trace 无 llm_call → 正确短路
预期调用 + Trace 无 llm_call   → 执行或采集异常
Trace 有 llm_call + 无 usage    → usage 采集缺失
```

### 4.2 调用次数比单次 Prompt 大小更容易被忽略

我原来的假设是：Tool Use 要把工具描述放进 Prompt，所以一定更贵。

12 个场景运行两轮后，结果相反：

| 架构 | 调用/请求 | Token/请求 | 平均耗时 | 通过率 |
|---|---:|---:|---:|---:|
| 固定链路 | 1.80 | 1,304.5 | 2,084ms | 0.8333 |
| Tool Use | 1.00 | 1,187.2 | 1,371ms | 1.0000 |

当前规模下，工具 Schema 增加的单次 Prompt 成本，小于固定链路多调用一次 Router 的成本。Tool Use 每个请求少 117 Token，端到端均值少 713ms。

这不是“Tool Use 永远更省”，而是说明：

> 架构成本由调用次数、每次 Prompt 大小和任务成功率共同决定，不能只盯工具 Schema。

Anthropic 对 workflow 与 agent 的建议也是先采用足够简单的方案，只在复杂度确实改善结果时升级，并明确指出 Agent 往往用更高的成本和延迟换任务表现：[Building effective agents](https://www.anthropic.com/engineering/building-effective-agents)。

### 4.3 `max_tokens` 不只是成本开关

两模型受控对比中，Flash 和 Pro 的总 Token 只相差约 0.6%，但 Pro 在 Extract 链路出现 8 次失败。

失败样本不是字段抽错，而是多动作 JSON 输出打满 `max_tokens=100` 后被截断：

```text
completion 达到 100 Token
→ JSON 不完整
→ Parser 返回 llm_parse_error
→ 预期写入 2～3 条，实际写入 0
```

这说明压低 `max_tokens` 可能减少单次输出，却同时降低任务完成率。模型切换时也不能只改 model name，必须重新验证输出长度、结构化协议和解析器兼容性。

### 4.4 Token 接近，不代表模型可以直接替换

两个模型在 Query 和 Tool Use 上都连续三轮全通过，但 Pro 还把“今天动了动腿”连续三轮从 `record` 错分为 `reject`。

如果只看总 Token，两个模型几乎持平；结合质量门禁后，Flash 才是当前更稳妥的默认模型。成本比较必须排在业务正确性和安全性之后。

## 5. 这次沉淀出什么方法

### 方法一：按任务建立成本证据链

```text
调用级：保存每次 usage
Case 级：沿 trace_id 汇总完整链路
实验级：按模型、架构和轮次聚合
决策级：与质量、安全和稳定性一起判断
```

### 方法二：成本比较必须冻结变量

比较架构或模型时，至少固定：

- Case 与 Fixture；
- Prompt 与代码提交；
- temperature、`max_tokens`、timeout、retries；
- 运行轮数和 ERROR 处理口径。

否则 Token 差异可能来自输入、参数或环境变化，而不是被比较的模型或架构。

### 方法三：先过门禁，再谈优化

```text
业务正确性 / 安全性未通过
→ 不进入成本优选

质量基本持平
→ 比较 Token、真实费用和延迟

Token 降低但任务失败率上升
→ 不算有效降本
```

## 6. 这份证据能证明什么、不能证明什么

能够证明：

- 当前项目已经实现调用级采集、Case 级聚合和模型级报告；
- 在 3 个短 Schema 工具的当前规模下，Tool Use 比固定链路少一次模型调用，总 Token 更低；
- `max_tokens` 会同时影响成本与结构化输出成功率；
- 同一套评测可以支持架构选择和模型选择，而不只是报通过率。

不能证明：

- Tool Use 在工具数量增加后仍然更省；
- Token 更少必然代表账单更低，模型单价和缓存命中尚未纳入；
- 当前端到端均值可以代表性能，节点耗时、P50/P95/P99 和并发尚未测；
- 当前内部评测集能够代表真实用户分布。

## 7. 你可以直接拿走什么

给其他 Agent 项目接成本评测时，先实现下面五项就够了：

1. 从每次真实模型响应读取 prompt、completion 和 total Token；
2. usage 写入 Trace，不污染业务返回；
3. 用唯一 `trace_id` 聚合同一任务的全部模型调用；
4. 同时报 `调用/Case`、`Token/Case`、任务成功率和 ERROR；
5. 只有质量门禁基本持平时，才用成本指标做架构或模型选择。

本章项目证据：

- [稳定性与成本口径评测报告](../eval/reports/稳定性与成本口径评测报告.md)
- [两模型受控对比评测报告](../eval/reports/两模型受控对比评测报告.md)

最终结论不是“统计 Token 很重要”，而是：

> Agent 成本的最小统计单位应该是完成一条任务的完整轨迹；脱离任务成功率的 Token 优化，没有业务意义。
