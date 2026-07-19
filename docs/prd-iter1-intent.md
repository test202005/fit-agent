# iter-1 专项 PRD — 意图识别

> 状态：v4——PRD 已确认；补充 confidence 数字字符串归一化的正式变更记录
> 上游：[prd.md](prd.md) v2（Phase 1 总需求）、[master-plan.md](master-plan.md) v3 Iteration 1 节
> 范围：仅意图路由。不含 extractor、存储写入、查询业务、HTTP 接口。本文只定业务口径，实现细节归 architecture。

---

## 1. 核心问题与交付

核心问题：一条用户输入，应该进入 `record / query / reject` 中的哪条链路？

交付：可脚本调用的 Router + 分层数据集（discovery / locked / regression）+ 评测 Runner + 第一份意图识别评测报告。

## 2. Router 契约（函数级，无 HTTP）

输入：`text`（用户原始输入，字符串）。

输出统一为判别结构：

```json
{"ok": true, "trace_id": "t-xxx", "intent": "record", "confidence": 0.93, "source": "llm"}
```

```json
{"ok": false, "trace_id": "t-xxx", "error_code": "bad_request | llm_timeout | llm_api_error | llm_parse_error"}
```

契约规则（定死）：

- `trace_id` 必返，成功失败都有；**`bad_request` 也生成 trace**，且其 trace 必须能证明未调用 LLM（防御路径可断言）
- 模型原始输出**只进 trace 不进业务返回**；`llm_parse_error` 的 trace 必须保留原始响应，否则解析失败不可定位
- 模型输出不满足约定 schema 的情况统一归 `llm_parse_error`：包括 intent 不在三枚举内、confidence 缺失/不可转为数字/越界、多余文本包裹。DeepSeek JSON Mode 偶发将 confidence 数字输出为数字字符串，Router 允许将0～1范围内的数字字符串归一化为 number；confidence 不参与决策，不让该格式噪声污染语义评测
- `source` 是业务语义（Phase 1 恒为 `llm`，为 iter-3 的 fallback 预留）；stub/live 用评测运行元数据 `run_mode` 区分，不混入 source
- confidence 模型自报，只记录不决策（继承总 PRD 决策 7）
- **iter-1 无降级路径**：LLM 失败即返回错误
- 入参防御：text 为空/纯空白、或超过 500 字符（按 Unicode 字符数）→ 不调 LLM，返回 `bad_request`
- 路由零业务副作用

## 3. 意图边界与标签决策表（评审重点）

### 3.0 record 最低事实门槛（本迭代最重要的一条规则）

> record = **明确陈述已发生的训练**，且至少包含一个可由下游抽取或追问的**训练对象**：具体动作、肌群/部位、训练类型、距离、时长，任一即可。

- 达标进 record："今天练了胸"（肌群）、"今天跑步了"（类型）、"刚做了半小时有氧"（类型+时长）
- 不达标进 reject："今天训练了"、"刚健身回来"、"今天练得不错"、"今天随便动了动"——有"练过"的语气但无训练对象，没有可补全的事实骨架

### 3.1 record 例句类型

| 类型 | 例句 |
|------|------|
| 标准句 | "今天卧推 60kg 做了4组每组8次" |
| 口语句 | "刚撸完铁，深蹲蹲了5组" |
| 多记录句 | "卧推60kg 4组每组8次，然后深蹲80kg 5组每组5次" |
| 混合句（事实+情绪） | "今天卧推60kg 4组，练完好累" |
| 相对日期句 | "昨天跑了5公里" |
| 缺字段事实 | "今天练了胸"——仍判 record，完整性由 iter-2 三态判定 |
| **否定转折句** | "今天没练卧推，改练深蹲5组"——含已发生正向事实，判 record |
| **未来+已发生混合** | "明天练腿，今天刚跑了5公里"——判 record，下游只抽已发生部分 |

### 3.2 query 例句类型

| 类型 | 例句 |
|------|------|
| 当日查询 | "今天练了什么" |
| 范围+动作 | "这周卧推了几次" |
| 相对日期查询 | "昨天的训练记录" |
| 部位查询 | "这周练了几次胸"——仍判 query，业务支持范围由 iter-3 判定 |

### 3.3 reject 例句类型

| 类型 | 例句 |
|------|------|
| 知识咨询 | "卧推标准动作是什么" |
| 建议请求 | "我该练什么" |
| 闲聊/情绪 | "你好"；"今天练得好累"（无训练对象） |
| 越界任务 | "帮我写周报" |
| 纯未来时态 | "明天打算练腿" |
| **纯否定/休息陈述** | "今天没练卧推，改成休息"——无正向事实，判 reject |
| 无对象泛化陈述 | "今天训练了"；"刚健身回来"（3.0 门槛不达标） |

### 3.4 标签决策表（已确认，作为标注与 prompt 唯一事实源）

| # | 规则 | 标签 |
|---|------|------|
| 1 | 已发生 + 含训练对象（3.0 门槛） | record |
| 2 | 缺字段事实（达 3.0 门槛但字段不全） | record（完整性下游判） |
| 3 | 否定/纠正之后含已发生正向事实 | record |
| 4 | 未来计划与已发生事实混合 | record（只抽已发生） |
| 5 | 纯否定、休息陈述，无正向事实 | reject |
| 6 | 纯未来时态 | reject |
| 7 | 已发生但无训练对象 | reject |
| 8 | 咨询 / 建议 / 闲聊 / 情绪 / 越界 | reject |
| 9 | 询问本人记录（含不支持的部位查询） | query |
| 10 | 单轮多意图复合句 | 不覆盖，独立 Backlog"单轮多意图拆分与执行顺序" |

写不出唯一 expected 的句子不进数据集，先回本表补规则。

## 4. 数据集要求（分层，`eval/` 下）

### 4.1 三个数据视图

| 视图 | 用途 | 使用规则 |
|------|------|---------|
| discovery/dev | 探索边界、调 prompt、跑 baseline | 可持续增删、可看单条结果 |
| locked eval | 迭代退出验收 | 阈值确定后**冻结**，不针对单条结果调 prompt |
| regression | 已修复的 badcase | 修复后长期保留，持续回归 |

一条 Case 可带多个用途标签，但必须遵守以下隔离规则：

- `discovery/dev` 与 `locked eval` 的 Case 必须互斥，`views` 不允许同时包含 `discovery` 和 `locked`
- discovery 中已修复的 badcase 可以进入 regression
- locked Case 一旦被用于针对性调整 prompt，就立即移出 locked、转入 regression，并补充新的 locked Case
- 报告必须区分 dev、locked 与 regression 的结果，不能混算

### 4.2 建设节奏与数量口径

- 探路集约 10 条：record 3、query 3、reject 3、高风险（纯否定/否定转折）1，先跑通 Runner
- 三分类正式集 ≥ 30 条：record ≥ 8（覆盖 3.1 全部类型，其中否定转折 ≥ 2）、query ≥ 6（覆盖 3.2）、reject ≥ 10（覆盖 3.3，其中纯否定 ≥ 3、无对象泛化 ≥ 2）、其余 ≥ 6 条向高风险边界类加密
- 故障注入 Case ≥ 3（timeout / api_error / parse_error 各 1）：**单列为契约与可靠性测试，不计入三分类准确率和混淆矩阵**
- 30 条定位是首版种子集，不对外称 benchmark
- 全部合成数据

### 4.3 Case schema

```json
{"case_id": "intent-record-001", "input": "今天卧推60kg 4组每组8次", "expected_intent": "record", "category": "record-standard", "views": ["discovery"]}
{"case_id": "intent-fault-001", "input": "今天卧推60kg 4组每组8次", "inject_fault": "llm_timeout", "expected_error_code": "llm_timeout", "category": "fault", "views": ["discovery"]}
```

## 5. 评测报告要求

- 三分类混淆矩阵 + 分类别 Precision / Recall + **macro-F1**（防类别不均衡掩盖问题），不只报总体准确率
- 解析错误率（llm_parse_error 占比）单独统计，不混入三分类指标
- 高风险类（纯否定、未来时态、咨询）"错进 record"条数单独呈现
- badcase 清单：输入、expected、actual、trace_id + 五层归因（边界定义 / prompt 表达 / 模型输出 / 解析器 / Case 标注），不允许一步归因到"模型能力"
- 版本快照：模型名与版本、prompt 内容哈希、采样参数、数据集版本与视图、运行时间、逐 Case trace_id
- 正式评测同一数据集连跑 3 次，呈现逐次结果与波动
- 两本账：stub 回归（`run_mode=stub`，契约+错误路径，零 token）与真实模型质量评测（`run_mode=live`）分开呈现

## 6. 退出流程与验收标准

### 6.1 阈值确定流程（定死）

```
discovery 集跑 baseline（3 次）
  → 主人基于 baseline 确认分类别 P/R 与 macro-F1 退出门槛（不凭空拍阈值）
  → 冻结 locked eval 集
  → locked 集连跑 3 次做退出验收
```

### 6.2 badcase 分级处置（替代"集外登记不阻塞"）

| badcase 类型 | 处置 |
|-------------|------|
| 范围内、稳定复现的高风险错误 | 立即入 regression 集，**修复并回归通过后才能退出** |
| 明确属于非目标的输入 | 登记已知限制，不阻塞 |
| 偶发、无法稳定复现 | 记录运行分布与影响，人工评估是否阻塞 |

### 6.3 验收标准（可判定）

1. Runner 一条命令跑完全量：stub 模式全绿（契约与错误路径断言全过）
2. locked 集 3 次运行完成，报告含混淆矩阵、分类别 P/R、macro-F1、解析错误率、版本快照
3. 高风险 Case 错进 record：locked 集 3 次运行均为 0
4. 分类别指标达到 6.1 流程确定的门槛
5. 任取一条失败 Case，凭 trace_id 还原到输入 / prompt 版本 / 模型原始输出 / 解析结果
6. 零写入以写入函数 spy 或文件系统快照验证（不只检查业务文件是否存在）；bad_request Case 的 trace 证明未调用 LLM

## 7. 非目标与已知限制

- 不做：extractor、三态判定、存储、查询业务、HTTP、任何降级规则
- 单轮多意图复合句不覆盖 → 独立 Backlog"单轮多意图拆分与执行顺序"（不是多轮问题，不归 Phase 2）
- confidence 无阈值、无决策作用
- 意图边界表（§3）是已经确认的 prompt 与标注唯一事实源：prompt 与本表冲突时改 prompt

## 8. 定死决策清单（评审重点）

1. record 最低事实门槛 = 已发生 + 至少一个训练对象（§3.0）
2. 否定转折含正向事实判 record；纯否定判 reject；未来+已发生混合判 record
3. 无对象泛化陈述（"今天训练了"）判 reject
4. Router 统一 ok/trace_id 判别结构；bad_request 入 error_code 枚举；模型原始输出只进 trace
5. schema 不符统一归 llm_parse_error；仅对 confidence 的0～1数字字符串做 number 归一化
6. stub/live 用运行元数据 run_mode，不占业务 source
7. 数据分三视图；discovery 与 locked Case 必须互斥，locked 集冻结后不针对单条调 prompt
8. 退出阈值由 discovery baseline 出来后主人确认，不凭空拍
9. 故障注入 Case 不进三分类指标
10. badcase 分级处置：范围内稳定复现必须修复回归，不得登记绕过
11. iter-1 无降级路径；空/超长输入（Unicode 字符数 > 500）不调 LLM

## 9. 变更记录

### v4（2026-07-19，主人确认）

- 正式采纳窄口径 confidence 归一化：仅允许把合法 JSON 中 0～1 的数字字符串转换为 number；
- confidence 不参与意图决策，归一化用于避免格式噪声污染语义指标；
- 非数字字符串、越界数字字符串和非法 JSON 仍返回 `llm_parse_error`；
- Runner 必须报告每轮 `confidence_normalized_count`，兼容行为可以存在但不能不可见；
- 本变更补齐 Iteration 1 执行期间未经正式记录的契约调整，经主人于 2026-07-19 明确批准。
