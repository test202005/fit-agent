# fit-agent PRD — Phase 1 单轮闭环

> 状态：v2——三个 P0 口径已确认，作为 Phase 1 总需求
> 范围：仅 Phase 1（单轮）。多轮补全、填槽、计划生成不在本文范围。
> 写法约定：每条需求给出明确 expected 和观测方式；所有边界决策在本文定死，不留给代码即兴发挥。

---

## 1. 产品概述

用户用自然语言对话记录和查询力量/有氧训练。系统识别意图（记录/查询/拒识），记录类输入抽取为结构化数据落盘，查询类输入返回汇总，其余拒识。

用户故事（Phase 1）：
1. 我说"今天卧推 60kg 做了4组每组8次"，系统立即记下这条训练，并复述记了什么（写入后回执，无确认环节）。
2. 我说"这周卧推了几次"，系统按记录回答（"几次" = 匹配的训练记录条数）。
3. 我说"帮我写周报"或纯吐槽，系统明确说这不归它管，不产生任何数据。

## 2. 接口协议

`POST /api/chat`

请求：`{"session_id": "s-xxx", "text": "用户输入"}`（session_id Phase 1 仅透传入 trace，不做状态）

响应（统一结构）：

```json
{
  "ok": true,
  "trace_id": "t-xxx",
  "intent": "record | query | reject",
  "intent_source": "llm | fallback",
  "confidence": 0.93,
  "status": "complete | incomplete | invalid | null",
  "reply": "给用户的自然语言回复",
  "data": {}
}
```

- `confidence`：模型自报，仅记录分析用，不参与任何决策
- `status`：仅 record 意图有值，其余为 null

### 错误响应契约

| 场景 | HTTP | error_code |
|------|:--:|------|
| 参数缺失/非法（无 text 等） | 400 | `bad_request` |
| LLM 超时，且无降级路径 | 200 | `llm_timeout` |
| LLM API 调用错误，且无降级路径 | 200 | `llm_api_error` |
| LLM 输出无法解析，且无降级路径 | 200 | `llm_parse_error` |
| 存储写入失败 | 500 | `storage_error` |
| 未捕获异常 | 500 | `internal_error` |

- `ok=false` 时响应只含 `{ok, trace_id, error_code, reply}`，**省略** intent / status / confidence / data（不是 null，是不出现）
- query 关键词降级成功不算错误：`ok=true, intent_source=fallback`
- 任何错误路径零写入

## 3. 意图定义

### 3.1 record（记录训练）

| 项 | 定义 |
|----|------|
| 触发语义 | 陈述已发生的训练事实 |
| 正例 | "今天卧推60kg 4组8次"；"昨天跑了5公里35分钟"；"深蹲 80 公斤做了5组，每组5个" |
| 反例（必须不触发写入） | "今天**没**练卧推，改成休息"（负向否定）；"明天打算练腿"（未发生）；"卧推60kg算重吗"（咨询）；"今天练得好累啊"（情绪，无量化事实 → reject 或 incomplete，见 3.4） |
| 行为 | extractor 抽取字段 → 三态判定 → complete **立即写入** workouts.jsonl（写入后回执，无用户确认环节；确认/修改/撤销进 Backlog）→ 回复复述记录内容 |
| expected | 见 §4 字段表 + §5 三态 |
| 观测方式 | 响应 `intent/status/data`；workouts.jsonl 追加行数；trace 节点 router→extractor→storage |

### 3.2 query（查询记录）

| 项 | 定义 |
|----|------|
| 触发语义 | 询问自己已有的训练记录 |
| 匹配语义 | 仅支持"日期范围 + 具体动作"；"几次" = 匹配的训练记录条数。肌群/部位词（"胸/腿/背"）Phase 1 不做映射：仍判 query，回复"暂不支持按部位查询，请说具体动作"，零副作用 |
| 正例 | "今天练了什么"；"这周卧推了几次"；"昨天的训练记录" |
| 反例 | "卧推怎么练"（知识咨询 → reject）；"我该练什么"（建议 → reject）；"这周练了几次胸"（部位查询 → 提示不支持） |
| 行为 | 解析日期范围/动作过滤 → 读 workouts.jsonl → 汇总回复。零副作用 |
| expected | 返回记录条数与内容和存储一致；无匹配时明确说"没有记录"，不编造 |
| 观测方式 | 响应 `data.items` 与 workouts.jsonl 过滤结果比对；trace 节点 router→query→storage(read) |

### 3.3 reject（拒识）

| 项 | 定义 |
|----|------|
| 触发语义 | 闲聊、情绪、越界任务（写周报/日报）、健身知识咨询、训练建议 |
| 正例 | "帮我写周报"；"你觉得我练得怎么样"；"卧推标准动作是什么" |
| 行为 | 一句话说明能力边界（只做记录和查询），零副作用、零写入 |
| expected | intent=reject，workouts.jsonl 零变化 |
| 观测方式 | 响应 intent；写入前后文件行数不变；trace 节点 router→reject |

### 3.4 边界决策（定死）

- 混合句（"今天卧推60kg 4组，练完好累"）：按 record 处理，情绪部分丢弃，只抽事实
- 一句多条记录（"卧推60kg 4组每组8次，然后深蹲80kg 5组每组5次"）：Phase 1 支持，抽取为多条记录，逐条三态判定，全部 complete 才批量落盘；任一条 incomplete/invalid 则整体不落盘，返回问题项（避免半写入）
- 纯情绪无事实（"今天练得好累"）：reject，不算 incomplete（没有可补全的事实骨架）
- 未来时态一律不记录：reject

## 4. 训练记录 schema（workouts.jsonl 每行）

| 字段 | 类型 | 必填 | 说明 |
|------|------|:--:|------|
| id | string | 系统 | `w-` + uuid，系统生成 |
| type | enum | 是 | `strength` \| `cardio`，由 extractor 判定 |
| exercise_raw | string | 是 | 用户原话中的动作名（"卧推"） |
| exercise_key | string \| null | 否 | 标准化键（"bench_press"），映射自动作词表 v1（约 20 个常见动作，见 architecture）；映射不到则 null，不影响 complete 判定 |
| weight_kg | number \| null | 否 | 自重训练可空；"60公斤/60kg/60千克"统一转 kg |
| sets | int | strength 必填 | 组数 |
| reps | int | strength 必填 | 每组次数；各组次数不同时 Phase 1 取用户明说的值，说不清则 incomplete |
| distance_km | number \| null | cardio 二选一 | 距离 |
| duration_min | number \| null | cardio 二选一 | 时长；distance/duration 至少其一，否则 incomplete |
| occurred_date | string | 是 | YYYY-MM-DD；缺省=今天；"昨天/前天"确定性换算；"上周"等模糊相对日期 → incomplete（缺 occurred_date） |
| source | enum | 是 | Phase 1 固定 `chat` |
| created_at | string | 系统 | ISO 时间戳 |
| trace_id | string | 系统 | 关联产生该记录的请求 |

校验规则（确定性代码校验，不信 LLM 输出）：weight_kg ∈ (0, 500]；sets ∈ [1, 20]；reps ∈ [1, 100]；distance_km ∈ (0, 200]；duration_min ∈ (0, 600]；occurred_date 不得晚于今天。越界 → invalid。

## 5. record 三态协议

| 状态 | 判定 | 副作用 | 响应 |
|------|------|--------|------|
| complete | 必要字段齐 + 全部通过 §4 校验 | 写入 workouts.jsonl | 复述记录内容 |
| incomplete | 是训练事实但缺必要字段 | **不落盘** | 指出缺什么（"缺组数次数"），提示补全（Phase 2 才做真正的多轮补全） |
| invalid | 字段越界 / 内容矛盾 / 无法解析 | **不落盘** | 说明无法记录及原因 |

## 6. 降级策略（LLM 调用失败/超时）

| 意图路径 | 降级行为 |
|---------|---------|
| 查询类关键词命中（"练了什么/记录/几次"） | 确定性规则查询，intent_source=fallback |
| 其余一切输入 | 返回可恢复错误提示（"暂时无法理解，请稍后再试"），**零写入** |

- record **没有降级写入路径**：没有 LLM 结构化抽取 + 确定性校验通过，任何输入不落盘
- 降级发生时 trace 记录 `fallback_reason`（timeout / api_error / parse_error）

## 7. 非功能需求

- **trace**：每请求生成 trace_id，节点最少覆盖 router / extractor（或 query）/ storage，每节点记录 in / out / 耗时 / 模型原始响应；JSON lines 落 `backend/logs/trace.jsonl`
- **隐私**：API key 等敏感字段不入日志；演示与评测数据全部合成；trace 写入失败不阻断主链路（降级为跳过，响应中不暴露）
- **性能**：每请求耗时记入 trace；Phase 1 不设性能验收门槛（口径无法稳定验收，先记录数据）
- **LLM stub**：llm.py 支持注入固定响应，评测 runner 可零 token 跑回归

## 8. 验收标准（可判定）

1. 数据集分迭代建设（`eval/intent-dataset.jsonl` → record 黄金集 → query 黄金集，Case schema 见 master-plan），Phase 1 收口时合计 ≥ 20 条，覆盖：三意图正例、负向否定、未来时态、混合句、多记录句、缺字段、字段越界、模糊日期
2. `python eval/run_eval.py` 一条命令复跑全部 Case，逐 Case 输出四层断言结果（intent / 字段级 / 副作用 / trace 节点）至 `eval/case-results.jsonl`
3. 副作用断言不只比行数：零写入类 Case（reject / incomplete / invalid / 降级 / 错误）跑完后 workouts.jsonl 零新增；写入类 Case 断言新增记录的字段值与 expected_record 逐字段一致、trace_id 与请求关联、多记录失败时零新增
4. 任取一条失败 Case，凭 trace_id 能还原到具体节点的 in/out
5. stub 模式全量跑通（deterministic regression），真实模型跑通并出分层准确率（model quality eval），两本账分开呈现

## 9. 已知限制（Phase 1 登记不解决）

- **不保证幂等**：客户端超时重试同一条 record 会产生重复记录；评测不覆盖重复请求场景
- 不支持肌群/部位查询、记录确认/修改/撤销（均在 Backlog）

## 10. 本文定死的决策清单（评审重点）

1. 多记录句"全部 complete 才落盘"，不做部分写入
2. 纯情绪归 reject 不归 incomplete
3. 模糊相对日期（"上周"）归 incomplete，不猜测
4. exercise_key 映射失败不影响 complete（宁可记下原文，不因词表小拒记）
5. reps 各组不同时不展开成逐组结构（Phase 1 简化，schema 预留演进空间）
6. record 无任何降级写入路径
7. confidence 不参与决策
8. query 收缩为"日期范围 + 具体动作"，"几次" = 匹配记录条数；肌群查询不支持（P0-1）
9. record 校验通过立即写入后回执，无用户确认环节（P0-2）
10. 错误响应契约见 §2；`ok=false` 时省略 intent / status / confidence（P0-3）
11. 评测运行向链路注入固定基准日期（`get_today()` 可注入），相对日期 Case 的 expected 均相对基准日期，保证跨日可复跑（实现见 architecture）
