# fit-agent 整体计划

> 状态：v3（Phase 1 拆为三迭代；stub 归入 iter-1，iter-1/2 不起 HTTP）
> 更新：2026-07-19
> 一句话：做一个自带测试基建的对话式健身训练 Agent——市面项目卖"我做了个 Agent"，这个项目卖"我做了个可测试的 Agent"。

---

## 定位与依据

- **产品对标**：Keep 卡卡（健身教练 Agent，2025.03 发布）、Fitbod（基于训练历史生成计划）、W8Log / FitLog AI（自然语言记录训练）。对话式记录 + AI 教练是已验证的产品方向，本项目是它的"开源可测试参考实现"。
- **数据闭环**：训练数据 = 对话记录（record 意图产生）+ 种子画像（评测用）；不接 HealthKit / 穿戴设备。
- **差异化**：评测闭环（数据集 → 执行 → 断言 → 逐 Case 结果）从 Phase 1 就存在，不是事后补。失败模式也是交付物——抽取不准、路由错分都是评测章节素材。
- **对外表述**（防概念过度包装）：可测试的对话式健身 Agent 应用参考实现，首阶段从**确定性 LLM workflow** 建立评测闭环，逐步走向 Agent。不把固定链路称作自主 Agent。依据：Anthropic《Building Effective Agents》——从简单可组合的 workflow 开始，业务需要时再加 Agent 复杂度。

## 与前身原型的关系

前身原型已归档。其问题：API 路径关键词伪路由、无上下文、无 trace、print 调试。本项目推倒新建，仅参考其 `run_once()` 的 function calling 写法。

---

## 目标架构

```
POST /api/chat {session_id, text}
  → session 载入（多轮 messages）              # Phase 2
  → Intent Router：LLM 意图识别                # Phase 1
      ├─ record → extractor：NL → 结构化记录 → 三态判定 → complete 才落盘
      ├─ query  → 按日期/动作查询汇总
      └─ reject → 闲聊/越界拒识
  → 降级矩阵（LLM 失败时按副作用分级降级）
  → trace：traceId 贯穿，每节点结构化 JSON 日志
  → eval：黄金集 + runner + 分层断言           # Phase 1 起
```

模块划分：

| 模块 | 职责 |
|------|------|
| `backend/server.py` | Flask 入口，参数校验和编排 |
| `backend/router.py` | 意图路由（LLM），输出 intent + confidence（confidence 只记录不决策） |
| `backend/extractor.py` | NL → 结构化记录 + 三态判定 |
| `backend/tools.py` | 存储读写（workouts.jsonl），仅接受 complete 记录 |
| `backend/session.py` | 多轮上下文管理（Phase 2） |
| `backend/trace.py` | traceId + 结构化日志 |
| `backend/llm.py` | LLM 统一入口，支持 stub 注入 |
| `eval/run_eval.py` | 批量 runner：加载数据集 → 调链路 → 断言 → 逐 Case 结果 |

### record 三态协议（Phase 1 定死，Phase 2 复用）

```
complete      必要字段完整且通过校验 → 落盘
incomplete    缺必要字段 → 不落盘，返回缺失字段清单（Phase 2 存入 session 走追问补全）
invalid       内容矛盾/无法解析 → 不落盘
```

### 降级矩阵（LLM 失败时）

| 意图 | 降级行为 |
|------|---------|
| query | 可降级到确定性规则查询 |
| reject | 返回能力边界提示 |
| record | **禁止仅凭关键词写入**；返回待确认结果，不落盘 |
| 不确定 | 返回可恢复错误，不落盘 |

所有降级在 trace 中记录降级原因。降级验收标准：LLM 失败时接口返回可恢复结果、**零错误写入**、降级原因可追溯——"接口不崩"只是最低要求。

### trace 隐私边界

- API key / Authorization 等敏感字段脱敏后才可入日志
- 公开演示和评测素材一律用合成数据，不用真实个人健康数据
- 日志写入失败不阻断业务主链路

## 意图集 v1

`record` / `query` / `reject` 三类。**延后**：profile 填槽、plan 生成、plan 微调。

---

## Phase 计划

### Phase 1：单轮链路 + 最小可执行评测闭环

Phase 1 是单轮产品闭环里程碑，不一次性开发完成。按“一个迭代只研究一个核心质量问题”的原则拆成三个迭代；每个迭代通过自己的可测试性门禁后，才启动下一个迭代。

当前 `docs/prd.md` 作为 Phase 1 总需求。每个迭代启动前，再基于总需求编写对应专项 PRD，不提前把后续实现细节写死。

#### Iteration 1：意图识别专项

核心问题：用户输入应该进入 `record / query / reject` 中的哪条链路？

功能范围：

- 定义三类意图及边界。
- 实现 Intent Router。
- 输出统一判别结构 `ok / trace_id / intent / confidence`，confidence 只记录不决策；模型原始输出只进 trace，不进业务返回。
- LLM 调用失败返回明确错误，不做业务写入。
- `llm.py` 统一 LLM 入口 + stub/故障注入（模拟超时、API 错误、输出格式错误，支撑错误路径测试与零 token 回归）。
- 建立 Router 级 trace 和最小批量评测 Runner。
- 不起 HTTP：评测 Runner 直调 router；`/api/chat` 到 Iteration 3 端到端收口时落地。

测试重点：三类基础正例、负向否定、未来时态、记录与咨询区分、记录与查询区分、混合表达、模糊表达、模型超时/API 错误/输出格式错误。

测试交付：

- 意图定义与边界表。
- `eval/intent-dataset.jsonl`。
- 意图评测 Runner。
- 分类别准确率和混淆矩阵。
- Bad Case 分类与第一份意图识别评测报告。
- 模型、Prompt、数据集和运行参数版本快照。

退出门禁：

- 每条 Case 都有唯一、明确的 `expected_intent`。
- 真实模型结果可以重复运行，概率性结果不以单次运行下结论。
- 三类意图分别统计，不只看总体准确率。
- 高风险 Case（纯否定、未来时态、咨询类）错进 record：locked 集 3 次运行均为 0。
- badcase 分级处置：范围内稳定复现的高风险错误必须修复并回归通过后才能退出；明确非目标的登记放行；偶发不可复现的人工评估（详见 prd-iter1-intent.md §6.2）。
- 分类别 P/R 与 macro-F1 达到基于 discovery baseline 确认的门槛，locked 集冻结后验收。
- 失败可通过 trace 定位到输入、Prompt、模型输出或解析环节。

Iteration 1 不实现 extractor、query 业务查询和 `workouts.jsonl` 写入。Router 判断为 record 也不产生业务副作用。

素材：#1 意图路由怎么测 + 为什么评测闭环要从第一天建。

#### Iteration 2：record 抽取与安全写入

核心问题：已经判定为 record 的输入，能否被正确抽取、校验并安全写入？

功能范围：extractor、训练记录 schema、`complete / incomplete / invalid` 三态、单位与日期处理、确定性校验、多记录原子写入（LLM stub 复用 iter-1 的 llm.py 入口，本迭代仍不起 HTTP）。

测试重点：字段级准确率、字段缺失、越界值、否定事实残留、多记录全部写入或全部不写、错误路径零写入。

测试交付：record 黄金集、字段级断言、副作用断言、Stub 确定性回归、真实模型抽取评测报告。

退出门禁：

- 必要字段和三态均有稳定 expected。
- 所有 incomplete、invalid 和异常路径零写入。
- 多记录输入不会发生部分写入。
- Stub 回归与真实模型质量评测分账呈现。

#### Iteration 3：query 与单轮端到端闭环

核心问题：用户能否按照确定的查询语义，准确读取已经写入的训练记录？

功能范围：日期范围解析、具体动作过滤、查询汇总、空结果、Router → query → storage 的端到端链路。

测试重点：查询范围、统计口径、无匹配结果、不编造数据、record 写入后可被 query 正确查出、全链路 trace。

测试交付：query 黄金集、查询断言、单轮端到端数据集、Phase 1 完整评测报告。

退出门禁：

- query 的匹配范围和统计口径已经确认。
- 查询结果与固定种子数据一致。
- 无匹配时明确返回空结果，不编造记录。
- 每条端到端 Case 可复跑、可判定、可凭 traceId 定位。

#### Phase 1 统一评测协议

Case schema 按迭代逐步扩展，每条至少包含当前迭代所需的 expected 和观测字段。完整单轮 Case 示例：

```json
{
  "case_id": "record-001",
  "input": "昨天卧推 60kg 4组，每组8次",
  "expected_intent": "record",
  "expected_record": {"exercise": "bench_press", "weight_kg": 60, "sets": 4, "reps": 8},
  "expected_side_effect": "append_one_record",
  "required_trace_nodes": ["router", "extractor", "storage"]
}
```

Phase 1 完成时统一统计：路由准确率 / 字段级准确率 / 副作用正确率 / trace 完整率 / 端到端成功率。

### Phase 2：多轮状态 + 多轮评测

功能范围：session 管理、`incomplete` 状态入 session、追问补全（补全后转 complete 落盘）、跨轮指代（"那昨天呢"）、上下文长度上限。

测试交付：多轮黄金集、补全正确性断言、错误挂接/状态污染用例、上下文截断用例、多轮 trace 回放。

素材：#2 多轮 Agent 怎么测（上下文丢失/错误挂接的断言设计）。

### Phase 3：评测工程化

交付范围：多类型 grader 组合、汇总报告、模型/prompt/数据集版本对比、失败聚类、可公开的评测报告和方法说明。

素材：#3 给 Agent 建评测流水线。

### Backlog（不承诺）

profile 填槽 → plan 生成 → seed 画像扩充 → 前端适配 → HealthKit 导入 → 记录确认/修改/撤销（PRD P0-2 移出）→ 肌群映射查询（PRD P0-1 移出）→ record 幂等保证 → 单轮多意图拆分与执行顺序（iter-1 review 移出）。

---

## 评测纪律

- **两本账分开统计，不得互相替代**：
  - deterministic regression：stub + 确定性断言，零 token，证明编排/契约/trace/存储没被破坏
  - model quality eval：真实模型 + 固定配置 + 黄金集，证明路由和抽取质量
  - stub 全绿 ≠ 模型质量回归通过
- **真实模型评测记录版本快照**：模型名称与版本、prompt 版本（或内容哈希）、采样参数、数据集版本、运行时间、每条 Case 的 traceId；关键 Case 输出随机性明显时重复运行，不用单次结果代表稳定表现
- **confidence 处理**：首版只记录用于分析，不作为写入/拒识门槛；黄金集积累后观察分区间真实准确率，再决定是否设阈值

## 编码启动门禁（Phase 1 动码前逐项确认）

- [x] 每个意图的输入、输出、错误协议已定义（→ prd.md v2 §2/§3）
- [x] record 必要字段与校验规则已定义（→ prd.md §4）
- [x] complete / incomplete / invalid 三态已定义（→ prd.md §5）
- [x] 任何降级路径不会错误写入（→ prd.md §6 降级矩阵）
- [x] 黄金集 Case schema 已定义（→ 本文档 + prd.md）
- [ ] Phase 1 有最小批量 runner，不靠人工执行（iter-1 交付）
- [ ] 路由 / 字段 / 副作用 / trace 分层断言已定义（随迭代逐层落地）
- [ ] stub 回归与真实模型评测分开统计（iter-1 起执行）
- [x] trace 脱敏与测试数据边界已定义（→ prd.md §7 + 本文档隐私边界）
- [x] 对外材料不把 Phase 1 描述为自主 Agent（→ 本文档对外表述）

## 非目标

- 不是健身产品：不追求训练计划专业性，公开发布注明"不构成训练建议"
- 不做：饮食、穿戴设备、社交、计划微调（暂缓）
- 不引入 LangChain 等 Agent 框架

## 节奏

慢迭代：每个 Phase 完成 → 主人亲手测一轮 → 沉淀素材 → 再启动下一个 Phase。不设 deadline。
