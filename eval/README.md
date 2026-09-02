# fit-agent AI 评测入口

> 给人和 AI 使用的测试总入口。先读本文件，再选择数据集、Runner、正式报告或方法文档。

## 1. 这里负责什么

`eval/` 负责 AI 能力效果评测：数据集、批量执行、指标计算、原始结果和正式结论。

它与其他目录的分工：

| 目录 | 职责 |
|---|---|
| `docs/` | 产品定位、PRD、架构和迭代计划 |
| `tests/` | 确定性代码单测，不调用真实模型 |
| `eval/` | Stub 契约回归和真实模型质量评测 |
| `backend/logs/` | Router Trace 原始事件 |

Iteration 1 只评测 `record / query / reject` 三分类意图路由，不评测 extractor、存储、查询执行、HTTP 或多轮状态。

## 2. 目录导航

```text
eval/
├── README.md
├── 意图识别评测计划.md
├── run_intent_eval.py
├── datasets/
│   └── intent-dataset.jsonl
├── methodology/
│   └── 意图识别数据集设计方法.md
├── reports/
│   └── 迭代一意图识别评测报告.md
└── results/
    ├── case-results-<timestamp>.jsonl
    └── report-<timestamp>.md
```

| 想做什么 | 入口 |
|---|---|
| 开始新一轮意图评测 | [意图识别评测计划](意图识别评测计划.md) |
| 看 Iteration 1 最终结论 | [正式报告](reports/迭代一意图识别评测报告.md) |
| 理解当前 100% 的适用边界 | [数据集设计方法](methodology/意图识别数据集设计方法.md) |
| 查看或扩充 Case | [意图数据集](datasets/intent-dataset.jsonl) |
| 查看评测实现 | [Runner](run_intent_eval.py) |
| 追溯某次运行 | `results/` 中的报告与逐 Case JSONL，再按 `trace_id` 查 `backend/logs/trace.jsonl` |

## 3. 数据集视图

当前同一个 JSONL 文件通过 `views` 区分用途：

| view | 当前用途 | 是否允许根据结果调 Prompt |
|---|---|:---:|
| `discovery` | Prompt 开发、能力探索和 baseline | 是 |
| `locked` | Iteration 1 小型验收集；不是严格独立盲测 | 验收阶段否；已暴露 Case 应转 regression |
| `regression` | 已修复 Bad Case 的长期回归 | 可以修，但 Case 永久保留 |

严格泛化评估需要后续新增独立 `blind_holdout`。具体 Review 项和执行门禁见[意图识别评测计划](意图识别评测计划.md)，分层依据见[数据集方法](methodology/意图识别数据集设计方法.md)。

Case 最小结构：

```json
{
  "case_id": "intent-record-001",
  "input": "今天卧推60kg做了4组每组8次",
  "expected_intent": "record",
  "category": "record-standard",
  "risk": "normal",
  "views": ["discovery"]
}
```

故障 Case 使用 `inject_fault` 和 `expected_error_code`，只在 Stub 模式执行，不计入三分类指标。

## 4. 怎么运行

先在项目根目录执行。

### 代码单测

```bash
.venv/bin/python -m pytest -q
```

### Stub 全量回归

```bash
.venv/bin/python eval/run_intent_eval.py --views all --run-mode stub
```

这本账验证 Router、Parser、Trace、Runner 和故障路径契约，不代表模型质量。

### 真实模型 discovery

```bash
.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode live --runs 3
```

### 真实模型 regression

```bash
.venv/bin/python eval/run_intent_eval.py --views regression --run-mode live --runs 3
```

### 真实模型验收集

```bash
.venv/bin/python eval/run_intent_eval.py --views locked --run-mode live --runs 3
```

Live 模式从项目根目录 `.env` 读取 `DEEPSEEK_API_KEY` 和可选的 `DEEPSEEK_MODEL`。不得输出、记录或提交密钥。

## 5. 报告怎么看

报告至少同时看：

- 样本量、view、run mode、模型、Prompt hash、数据集 hash；
- 端到端通过率、分类别 Precision/Recall/F1、macro-F1；
- parse error、timeout 和高风险错进 `record`；
- 每条 Case 的 expected、actual、trace_id；
- 多次运行是否稳定，而不是只看最好的一次。

`results/` 是本地自动生成的原始证据，按 `.gitignore` 不进版本库。`reports/` 是人工核对后的正式结论，进入版本库。对外引用优先使用正式报告，但必须保留它指向的模型、Prompt、数据集和原始结果快照。

## 6. 修改规则

1. 业务标签边界变化：先改 `docs/prd-iter1-intent.md`，再改 Prompt 和数据集。
2. Prompt 调优：先跑 discovery；不得针对仍标为 locked 的单条结果调优。
3. 新 Bad Case：先归因；范围内稳定问题修复后加入 regression。
4. 数据集修改：保持 discovery 与 locked 互斥，更新 Case ID、expected、category、risk 和 views。
5. Runner 修改：跑单测和 Stub 全量，确认零业务写入和 Trace 契约没有退化。
6. 正式结论变化：更新 `reports/`，不要拿 `results/` 中某一份中间报告直接替代最终结论。

## 7. 当前状态

Iteration 1 工程闭环已经完成。当前准确口径是：16 条小规模验收集连续三轮全部通过；这不等于真实用户分布下的意图准确率为 100%。详情见[正式报告](reports/迭代一意图识别评测报告.md)和[数据集方法](methodology/意图识别数据集设计方法.md)。新一轮评测从[意图识别评测计划](意图识别评测计划.md)开始 Review。
