# iter-2 专项 PRD — 抽取与受控写入

> 状态：v1 待评审
> 上游：[prd.md](prd.md) v2 Phase 1 总需求、[master-plan.md](master-plan.md) v3
> 前置：[iter-1 意图识别 PRD](prd-iter1-intent.md) v4、[存储选型与企业实践差异](存储选型与企业实践差异.md)
> 范围：`record` 意图之后的抽取、三态判定与落盘。不含查询执行、HTTP、多轮追问。

---

## 1. 核心问题与交付

**核心问题**：一句被判为 `record` 的自然语言，能不能变成一条可信的训练记录并安全落盘？

**为什么单独成一个迭代**：这是本项目第一次产生**副作用**。意图判错，用户重说一遍即可；**抽取判错会往用户的训练记录里写一条假数据**——他明天翻记录，看到自己昨天卧推了 600kg，而且不会有人报 bug。

**交付**：extractor 节点 + 三态判定 + JSONL 落盘 + 评测集与断言扩展 + iter-2 评测报告。

## 2. 链路

```
text → [iter-1] Router → intent=record → [新] Extractor → 三态判定 → [新] 受控写入 → 回执
                       ↘ query / reject → 本迭代不处理，原样返回
```

**第二个 LLM 节点**是本迭代的关键变化：一条 Case 失败时，必须能分清**是意图层错了还是抽取层错了**。这决定了 trace 与断言的设计（见 §6）。

## 3. 抽取契约

### 3.1 目标字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `exercise` | string | 动作名，如「卧推」「跑步」 |
| `weight_kg` | number \| null | 重量 |
| `sets` | number \| null | 组数 |
| `reps` | number \| null | 每组次数 |
| `duration_min` | number \| null | 时长，有氧类常用 |
| `distance_km` | number \| null | 距离，有氧类常用 |

除 `exercise` 外全部可空。**不做单位换算、不做动作名归一化**（「卧推」和「杠铃卧推」不合并）——归一化需要动作词典，属于 Backlog。

### 3.2 输出结构

```json
{"ok": true, "trace_id": "t-xxx", "state": "complete",
 "records": [{"exercise": "卧推", "weight_kg": 60, "sets": 4, "reps": 8,
              "duration_min": null, "distance_km": null}]}
```

```json
{"ok": false, "trace_id": "t-xxx", "error_code": "llm_parse_error"}
```

沿用 iter-1 的判别结构与错误码枚举，不新增错误码类型。

## 4. 三态判定（本迭代最重要的规则）

> 判定依据：这条记录**能不能构成一条有意义的训练记录**。

| 状态 | 门槛 | 例子 |
|---|---|---|
| `complete` | 有 `exercise` **且**至少一个量化字段非空 | 「今天卧推60kg做了4组每组8次」「昨晚跑了五公里」「刚做完半小时有氧」 |
| `incomplete` | 有 `exercise`，但量化字段**全为空** | 「今天练了胸」「今天做了深蹲」 |
| `invalid` | 抽不出 `exercise` | 「今天练得挺爽」（理论上不会到这，因为 iter-1 已拦掉，但必须兜住） |

**决策 1**：有氧类的量化就是**时长或距离**，不要求重量组数次数。「昨晚跑了五公里」是 `complete`。

**决策 2**：`invalid` 是**防御性状态**。iter-1 的 `reject` 应已拦掉这类输入，但**两层判断不能互相假设对方一定正确**——上游变松时下游必须兜住。

## 5. 写入规则

**决策 3**：`complete` 与 `incomplete` **都写入**，用 `state` 字段标记；`invalid` **不写**。

理由：用户说了就是想记，丢掉等于系统吃了他的数据；标记出来将来可追问补全。代价是库里会有不完整记录，**因此 `state` 必须落盘，查询侧才能区分对待**。

**决策 4**：一句话多个动作**拆成多条记录**。「卧推60kg4组，然后深蹲80kg5组」→ 两条。因为查询要按动作统计，不拆后面全是坑。

### 5.1 落盘格式

追加写入 `data/records.jsonl`，一条一行：

```json
{"id":"r-20260902-001","ts":"2026-09-02T20:10:00+08:00","state":"complete","trace_id":"t-xxx",
 "exercise":"卧推","weight_kg":60,"sets":4,"reps":8,"duration_min":null,"distance_km":null}
```

**每条记录必须带 `trace_id`**——出现脏数据时能反查回那次 Agent 调用。这是为将来换存储保留的能力，不是可选项。

### 5.2 明确不做

- **不做幂等/去重**：同一句话说两次写两条。列为已知限制。
- **不做事务**：单文件追加，写一半的情况通过整行写入规避。
- **不做删除与修改**：本迭代只追加。
- **不引入数据库**：理由见 [存储选型与企业实践差异](存储选型与企业实践差异.md)。

> 企业里这四条都必须做，且是测试重点。本项目**明确不做并写进已知限制**，不是遗漏。

## 6. 可观测性与归因（两个 LLM 节点的必然要求）

trace 事件扩展，`node` 字段区分层：

```
router:    input_received → llm_request → llm_response → parse_result → result
extractor: extract_request → extract_response → parse_result → state_decided
storage:   write_attempted → write_result
```

**同一个 `trace_id` 贯穿三层**，不允许各层各生成一个。

**失败必须能归到具体层**：

| 现象 | 归因层 |
|---|---|
| 意图判成了 `reject`，压根没进抽取 | router |
| 进了抽取但字段抽错 | extractor |
| 抽对了但三态判错 | 判定规则 |
| 判定对了但没写/写错 | storage |

## 7. 评测要求

沿用 iter-1 的 Runner、三视图、四态 Verdict 与双口径报告，**扩展断言**：

| 断言层 | 本迭代新增 |
|---|---|
| 第一问 · 契约 | `state` 在三枚举内；`records` 是数组；字段类型正确 |
| 第二问 · 目标 | 抽取字段逐个比对；`state` 判定正确；记录条数正确 |
| 第三问 · 保护 | **`invalid` 必须零写入**；写入条数与预期一致，不多不少；**不修改已有记录** |
| 第四问 · 一致 | 落盘内容与返回的 `records` 一致；`trace_id` 可回查 |

新增断言类型：`fields_equal`（多字段比对）、`state_equals`、`record_count_equals`、`no_write`。

**数据集**：新增 extractor 视图，约 25–30 条，覆盖 complete / incomplete / invalid 三态、有氧与力量、单动作与多动作、以及**故意的干扰**（数字出现在非量化位置，如「练了3年的卧推」）。

**新增副作用断言**：每条 Case 跑前跑后对 `data/records.jsonl` 做快照，比对**新增行数与内容**——不再只看有没有意外写入，而是精确断言写了什么。

## 8. 退出标准

1. Runner 一条命令跑完，stub 全绿
2. Live 三视图各连跑 3 轮，报告含三态混淆矩阵与写入断言结果
3. **`invalid` 零写入，三轮均为 0**
4. **写入条数错误为 0**（多写、少写都算失败）
5. 任取一条失败 Case，凭 `trace_id` 能定位到是 router / extractor / 判定 / storage 哪一层
6. 单测覆盖三态边界与写入防御

## 9. 定死决策清单（评审重点）

1. 三态门槛 = 有 `exercise` + 至少一个量化字段（有氧用时长/距离）
2. `complete` 与 `incomplete` 都写入并标记 `state`，`invalid` 不写
3. 一句多动作拆成多条记录
4. 不做幂等、不做事务、不做删改、不引入数据库——全部列为已知限制
5. 每条记录必带 `trace_id`
6. 同一 `trace_id` 贯穿 router / extractor / storage 三层
7. 不做单位换算与动作名归一化
8. `invalid` 是防御性状态，不假设上游一定正确

## 10. 非目标

查询执行、HTTP 接口、多轮追问补全、动作词典、单位换算、数据修改与删除、并发写入。
