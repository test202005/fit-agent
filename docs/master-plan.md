# fit-agent 整体计划

> 状态：**v4**（Phase 1 已完成四个迭代；规划依据改为「按能力阶梯 + 岗位要求倒推」）
> 更新：2026-09-03
> 一句话：做一个自带测试基建的 Agent——市面项目卖"我做了个 Agent"，这个项目卖"我做了个**可测试的** Agent，并且能说清什么时候不该用 Agent"。
> 恢复入口：[当前进度](../当前进度.md)｜规划全景：[Agent 能力与评测全景](Agent能力与评测全景.md)

---

## 定位与依据

- **产品对标**：Keep 卡卡（健身教练 Agent，2025.03 发布）、Fitbod（基于训练历史生成计划）、W8Log / FitLog AI（自然语言记录训练）。对话式记录 + AI 教练是已验证的产品方向，本项目是它的"开源可测试参考实现"。
- **数据闭环**：训练数据 = 对话记录（record 意图产生）+ 种子画像（评测用）；不接 HealthKit / 穿戴设备。
- **差异化**：评测闭环（数据集 → 执行 → 断言 → 逐 Case 结果）从 Phase 1 就存在，不是事后补。失败模式也是交付物——抽取不准、路由错分都是评测章节素材。
- **对外表述**（防概念过度包装）：可测试的对话式健身 Agent 应用参考实现，首阶段从**确定性 LLM workflow** 建立评测闭环，逐步走向 Agent。不把固定链路称作自主 Agent。依据：Anthropic《Building Effective Agents》——从简单可组合的 workflow 开始，业务需要时再加 Agent 复杂度。

### 系统设计总原则

项目不为了展示名词增加组件，而是从大模型的工作特点和真实业务缺口倒推系统设计：

```text
模型能力边界
→ 当前业务为什么做不到或不稳定
→ 引入最小系统组件
→ 识别新增失败模式
→ 增加可观测点、Case、断言和回归
→ 用真实结果决定是否保留
```

当前已经实践的前置组件包括 Prompt、JSON 输出契约与严格解析、统一 LLM 接口、Trace、Tool Use 和持久化。Prompt 已有独立文件、内容 hash、Git commit 和报告快照等基础版本证据，但还没有版本注册、候选/生产切换和自动回滚能力，不称为完整 Prompt 管理系统。Skill、MCP、RAG、Memory 与多步 Agent 只有在出现真实需求、可解释失败模式和可复跑证据时才进入迭代，不作为固定升级清单。

## 与前身原型的关系

前身原型已归档。其问题：API 路径关键词伪路由、无上下文、无 trace、print 调试。本项目推倒新建，仅参考其 `run_once()` 的 function calling 写法。

---

## 当前架构（v4 实际实现）

两条并行链路，执行层共用：

```
POST /api/chat {text}
  │
  ├─ 固定链路（iter-1~3）
  │    Router 判意图 ─┬─ record → Extractor → 三态判定 → 受控写入
  │                   ├─ query  → Planner(LLM) → Executor(纯代码)
  │                   └─ reject → 直接返回
  │
  └─ 工具调用链路（iter-4）
       模型看工具清单 → 自己选工具与参数 → 执行 → trajectory

  贯穿：同一 traceId 覆盖全部节点，每节点结构化 JSON 日志
  评测：四套 runner，stub 与 live 两本账分开统计
```

**两条链路并存是刻意的**：固定链路是架构对比实验的对照组，也是模型一个工具都不选时的兜底。

模块划分：

| 模块 | 职责 |
|------|------|
| `backend/app.py` | Flask 入口，`POST /api/chat` + 异常兜底 |
| `backend/pipeline.py` | 固定链路编排：router → extractor/planner → storage |
| `backend/router.py` | 意图路由（LLM），confidence 只记录不决策 |
| `backend/extractor.py` | NL → 结构化记录 + 三态判定 |
| `backend/query.py` | Planner（LLM 翻译条件）+ Executor（纯代码执行） |
| `backend/agent.py` | 工具调用循环，单轮上限 3 次 |
| `backend/tools.py` | 工具定义、参数校验、执行器工厂 |
| `backend/storage.py` | `StorageClient` Protocol + SQLiteStorage / JsonlStorage / FakeStorage |
| `backend/clock.py` | `Clock` Protocol + SystemClock / FrozenClock |
| `backend/llm.py` | LLM 统一入口，含工具调用；LiveLLM / StubLLM |
| `backend/trace.py` | traceId + 结构化日志 |
| `eval/run_*_eval.py` | 四套 runner：意图 / 抽取 / 查询 / 工具 |
| `eval/run_architecture_compare.py` | 双架构对比实验 |

**三个统一入口，三对真假实现**——判据：任何让测试变慢、变贵或不确定的依赖，都必须能被替换掉。

```
LLMClient  →  LiveLLM       / StubLLM
Storage    →  SQLiteStorage / JsonlStorage / FakeStorage
Clock      →  SystemClock   / FrozenClock
```

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

## 已完成：Phase 1（四个迭代）

原计划拆三个迭代，实际做了四个——第四个（工具调用）是在做完全景图、对照岗位要求后新增的，因为它是 workflow 与 Agent 的分界线。

| 迭代 | 核心问题 | 交付 | tag |
|---|---|---|---|
| **iter-1** 意图识别 | 一句话该进哪条链路 | 三分类路由 + trace + stub + 三视图评测集 | `v1.0`–`v1.3` |
| **iter-2** 抽取与写入 | 怎么变成结构化记录并安全落盘 | extractor + 三态判定 + 受控写入 | `v2.0` |
| **iter-3** 查询与端到端 | 写进去的怎么读出来 | Planner/Executor 分离 + Clock 注入 + HTTP | `v3.0` |
| **iter-4** 工具调用 | 模型自己决定做什么 | 工具调用 + trajectory 断言 + 架构对比 | `v4.0` |

**当前能力层：L1 Tool Use**（此前为 L0 固定 workflow，层级定义见全景图）。

### 当前状态

- 单元测试 140 passed；四套 stub 全绿（意图 58 / 抽取 28 / 查询 22 / 工具 28）
- Live 三视图各连跑 3 轮，四套全部通过
- 三条硬门禁常态为 0：参数幻觉、不该调却调工具、不该写却写

### 四轮里最值钱的三个产出

1. **方法论**：[断言方法论](../eval/methodology/断言方法论.md)（四问 / 四态 / 三级漏斗）、[数据集方法论](../eval/methodology/数据集方法论.md)（六步 / Golden 准入 / 回流 / 脱敏）——先沉淀，再用它回扫自己，扫出了 V1.0 里一条**永远不会失败的断言**
2. **架构对比实测**：同一批输入、执行层相同，单一意图两者持平且工具调用更快，复合请求固定链路结构性失败——**把「该不该上 Agent」从口头争论变成带数据的结论**
3. **问题账本**：[问题清单](../eval/reports/问题清单.md) 六条，含现象、修法与**没选的方案**；[版本演进复盘](../eval/reports/版本演进与问题复盘.md) 记录每版发现了什么

---

## 后续方向（粗粒度，不锁死）

排序依据三条：**补岗位要求的空白 → 失败模式能否确定性断言 → 实现成本**。详见 [全景图](Agent能力与评测全景.md) 第五节。

### 近期（下一到两轮）

| 方向 | 为什么 | 大致范围 |
|---|---|---|
| **V6 SQLite 持久化与数据一致性** | Agent 已经产生真实记录，需要补企业级状态基础 | **已完成**：默认本地 SQLite；已验证持久化、隔离、事务、重复请求和恢复 |

两模型受控对比已于 V5.1 完成，结果见[两模型受控对比评测报告](../eval/reports/两模型受控对比评测报告.md)。下一轮 PRD 见[V6 SQLite 持久化与数据一致性](prd-v6-persistence.md)。

### 中期

| 方向 | 为什么 | 注意 |
|---|---|---|
| **L2 ReAct 多步循环** | 第一次出现代码断不了的失败模式（推理与行动不一致） | 也是 LLM 裁判方法论的**首次实施时机**——方法论早写好了，缺一次落地 |
| **L5 多轮 Memory** | 失败模式大部分可确定性断言，且产品上真实需要（iter-2 的 `incomplete` 本就等着追问补全） | 原 Phase 2 内容 |

### 远期锚点（不承诺，写在这里是知道靶子在哪）

- **评测工程化**：多类 grader 组合、失败聚类、版本对比报告（原 Phase 3）
- **L3 Reflection / L4 Planning**：依赖 L2 成熟
- **L6 多 Agent**：单 Agent 评测都没做透，上多 Agent 是跳级

### 明确不做

| 项 | 理由 |
|---|---|
| **评测平台（Web 界面）** | 现在做是自嗨。触发条件：**有第二个人需要用这套评测** |
| **MCP / Agent 框架** | 手写循环才看得见每一层怎么坏；框架藏掉的正是要测的东西 |
| **多模态评测** | 另有项目承载，不在本仓 |
| **blind holdout** | 日常迭代不需要，只有对外宣称准确率数字时才建 |

### Backlog（不承诺）

profile 填槽 → plan 生成 → seed 画像扩充 → 前端适配 → HealthKit 导入 → 记录确认/修改/撤销 → 肌群映射查询 → record 幂等保证 → 单轮多意图拆分与执行顺序（iter-4 工具调用已部分解决）。

---

## 规划原则（v4 新增，比 Phase 划分更重要）

**先立靶子，再倒推能力。** 迭代顺序由「岗位要什么 + 哪块是空白」决定，不由「链路还差什么功能」决定。

三条判断：

1. **能穷举的分支写死，穷举不完的交给模型**——判断依据见 [何时该用 Agent](何时该用Agent.md)
2. **能用代码断死的层优先做**——能力越往上越依赖裁判，成本与不确定性同时上升
3. **每层都要先把确定性断言榨干**，再考虑上裁判

---

## 评测纪律

- **两本账分开统计，不得互相替代**：
  - deterministic regression：stub + 确定性断言，零 token，证明编排/契约/trace/存储没被破坏
  - model quality eval：真实模型 + 固定配置 + 黄金集，证明路由和抽取质量
  - stub 全绿 ≠ 模型质量回归通过
- **真实模型评测记录版本快照**：模型名称与版本、prompt 版本（或内容哈希）、采样参数、数据集版本、运行时间、每条 Case 的 traceId；关键 Case 输出随机性明显时重复运行，不用单次结果代表稳定表现
- **confidence 处理**：首版只记录用于分析，不作为写入/拒识门槛；黄金集积累后观察分区间真实准确率，再决定是否设阈值

## 编码启动门禁（Phase 1 已全部关闭）

- [x] 每个意图的输入、输出、错误协议已定义（→ prd.md v2 §2/§3）
- [x] record 必要字段与校验规则已定义（→ prd.md §4）
- [x] complete / incomplete / invalid 三态已定义（→ prd.md §5）
- [x] 任何降级路径不会错误写入（→ prd.md §6 降级矩阵）
- [x] 黄金集 Case schema 已定义（→ 本文档 + prd.md）
- [x] Phase 1 有最小批量 runner，不靠人工执行（四套 runner 已交付）
- [x] 路由 / 字段 / 副作用 / trace / trajectory 分层断言已定义并落地
- [x] stub 回归与真实模型评测分开统计（四套 runner 均执行）
- [x] trace 脱敏与测试数据边界已定义（→ prd.md §7 + 本文档隐私边界）
- [x] 对外材料不把 Phase 1 描述为自主 Agent（→ 本文档对外表述）

## 非目标

- 不是健身产品：不追求训练计划专业性，公开发布注明"不构成训练建议"
- 不做：饮食、穿戴设备、社交、计划微调（暂缓）
- 不引入 LangChain 等 Agent 框架——手写循环才看得见每一层怎么坏

## 节奏

慢迭代：**一个迭代只跨一个能力台阶**，做完 → 复盘记账 → 再启动下一个。不设 deadline。

每轮固定动作：写专项 PRD → 定死口径 → 动码 → 单测 + stub + live 三视图三轮 → Review 记问题清单 → 复盘 → 打 tag。
