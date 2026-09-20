# fit-agent

**一套可以直接抄走的 LLM Agent 评测脚手架**--trace-first、零 token 回归、分层数据集、故障注入。用一个最小的对话式健身记录 Agent 当载体，因为方法要跑在真实链路上才说得清。

如果你正在写 Agent，但不知道怎么证明它「改一版没变坏」，这个仓库是给你看的。

---

## 它解决什么问题

LLM 的输出不确定、调用要花钱，于是大多数 Agent 项目的测试停在「手动跑几条看看对不对」。这个仓库把它变成可重复的工程：

| 问题 | 这里的做法 |
|---|---|
| 每跑一次回归都要花钱 | 所有 LLM 调用走统一入口，回归注入 stub，**零 token** |
| 模型每次输出都不同，没法当回归 | 拆两本账：契约回归（确定性、免费）与质量评测（真调模型、单独报） |
| 挂了不知道错在哪一步 | 每次调用落结构化 trace，模型收到什么、返回什么原文全部可回溯 |
| 造多少条数据才够 | 不数条数，数**意图等价类**覆盖；规则表先行，写不出唯一 expected 的句子不进数据集 |
| 调 prompt 的数据拿来验收 = 自己给自己判卷 | 数据集分三视图：discovery / regression / locked，互斥且分开算 |
| 只测好输入 | 主动注入超时、接口错误、坏 JSON，验证系统兜得住 |

---

## 快速开始

### 1. 环境

- Python 3.9 及以上（在 3.9.6 上验证过；命令按 macOS / Linux 书写）
- 三个依赖：`openai`、`pytest`、`flask`
- 真实模型评测需要一个 [DeepSeek](https://platform.deepseek.com) API key；**零 token 回归不需要**

### 2. 安装

```bash
git clone <this-repo> fit-agent && cd fit-agent
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. 零 token 跑通（不需要 key）

```bash
.venv/bin/python -m pytest -q                                            # 219 条确定性单测

.venv/bin/python eval/run_intent_eval.py  --views all --run-mode stub   # 意图识别 58 条
.venv/bin/python eval/run_extract_eval.py --views all --run-mode stub   # 抽取与受控写入 28 条
.venv/bin/python eval/run_query_eval.py   --views all --run-mode stub   # 查询规划与执行 22 条
.venv/bin/python eval/run_tool_eval.py    --views all --run-mode stub   # 单轮 Tool Use 28 条
.venv/bin/python eval/run_plan_eval.py    --views all --run-mode stub   # 训练计划生成 8 条 + 2 条故障检测
```

看到 `pytest` 全绿、五个 Runner 无 FAIL / ERROR 即跑通。退出码约定：只有 `PASS / REVIEW` 返回 0，出现 `FAIL / ERROR` 返回 1。

### 4. 接真实模型

```bash
cp .env.example .env
```

编辑 `.env`，填入自己的 key：

```dotenv
DEEPSEEK_API_KEY=your-key
DEEPSEEK_MODEL=deepseek-v4-flash
```

```bash
# 真调模型，探路集连跑三轮（会花 token）
.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode live --runs 3
```

- 五个 Runner 通用参数：`--runs`（多轮）、`--models a,b`（多模型对比，仅 live）、`--temperature`（仅稳定性专项用，升温结果不是质量结论）。
- 计划生成 Runner 单轮 discovery Live 约 7,500 token，其他 Runner 用量以各自报告的 token 汇总为准。
- 没配 key 就跑 live 会直接报 `DEEPSEEK_API_KEY is not configured`，不会静默降级到 stub。
- `.env` 由 `eval/` 下的 Runner 自动读取；`.env`、trace 和评测结果均已在 `.gitignore` 中。

### 5. 跑起完整服务（可选）

服务入口 **不会** 自动读取 `.env`，需要先把变量导入当前 shell：

```bash
set -a; source .env; set +a
.venv/bin/python -m backend.app          # 默认 5001 端口，可用 PORT 覆盖

curl -X POST localhost:5001/api/chat -H 'Content-Type: application/json' \
     -d '{"text":"今天卧推60kg做了4组每组8次","user_id":"demo-user","request_id":"req-001"}'
curl -X POST localhost:5001/api/chat -H 'Content-Type: application/json' \
     -d '{"text":"今天练了什么"}'
curl localhost:5001/health
```

数据落在 `data/records.sqlite3`（SQLite，自动创建）。`/api/chat` 覆盖意图路由、记录写入和查询；训练计划生成目前只通过 `eval/run_plan_eval.py` 运行，没有 HTTP 入口。

### 6. 结果去哪看

| 内容 | 位置 |
|---|---|
| 评测报告与逐 Case 结果 | `eval/results/`（自动生成，Git 忽略） |
| 每次调用的 trace 原始事件 | `backend/logs/trace.jsonl` |
| 人工核对过的正式结论 | `eval/reports/` |

---

## 五套评测一览

| 评测 | Runner | 数据集 | 验证什么 |
|---|---|---|---|
| 意图识别 | `run_intent_eval.py` | `intent-dataset.jsonl` | record / query / reject 三分类、高风险不入库 |
| 抽取与受控写入 | `run_extract_eval.py` | `extract-dataset.jsonl` | complete / incomplete / invalid 三态、写入次数 |
| 查询规划与执行 | `run_query_eval.py` | `query-dataset.jsonl` | 查询类型、时间边界、只读 |
| 单轮 Tool Use | `run_tool_eval.py` | `tool-dataset.jsonl` | 选对工具、参数不幻觉、不该调时不调 |
| 训练计划生成 | `run_plan_eval.py` | `plan-dataset.jsonl` | planner → tool → generator 三步，黑盒看结果，白盒看中间层 |

另有 `run_architecture_compare.py`：同口径对比两种架构的通过率、耗时、调用次数和 token（真实模型，会花 token）。

---

## 你会看到什么

报告写到 `eval/results/report-<timestamp>.md`，长这样：

```
- end_to_end_pass_rate: 1.0000
- macro_f1: 1.0000
- parse_errors: 0
- high_risk_into_record: 0

| label  | precision | recall |     f1 |
| record |    1.0000 | 1.0000 | 1.0000 |
| query  |    1.0000 | 1.0000 | 1.0000 |
| reject |    1.0000 | 1.0000 | 1.0000 |
```

外加混淆矩阵、逐 Case 的 `trace_id`、badcase 清单，以及版本快照（模型、git commit、prompt hash、数据集 hash、全部采样参数）--**每份报告都可复现、可对比**。

拿任意一条失败 Case 的 `trace_id` 去 `backend/logs/trace.jsonl` 一查，五个事件还原整条链路：

```
input_received → llm_request → llm_response → parse_result → result
```

真实例子：某条 case 失败，只看结果像是「模型分错了」；查 trace 发现模型返回的是 `{"intent":"record","confidence":0.95"}`--**分类是对的，多了一个引号导致 JSON 非法**。归因从「模型能力不行」变成「prompt 的格式约束不够死」，修法完全不同。

---

## 四个可以直接抄的设计

**1. LLM 走统一入口，故障注入只在这一层**

```python
class LLMClient(Protocol):
    def complete(self, system_prompt: str, user_text: str) -> LLMResult: ...

# LiveLLM  → 真调模型，SDK 异常在入口翻译成自家异常
# StubLLM  → 测试替身，注入固定响应或指定故障
```

业务代码永远不 import SDK 的异常类型；换模型厂商不用改被测代码一行。

**2. 两本账分开报**

stub 跑的是**契约**（解析、错误映射、trace 完整性、无副作用），不是模型质量--报告头部就写着这句免责声明。别把它当准确率。

**3. 数据集三视图 + 双层断言**

每条 case 除了断言结果，还断言 **trace 事件集是否齐全**。比如输入防御失败的 case，必须证明它的 trace 里**没有** `llm_request` 事件--「返回对了」和「真的没花钱」是两件事。

**4. 评测不许污染被测系统**

runner 跑前跑后对源码目录做 mtime 快照比对，有意外写入直接报错退出。

---

## 怎么改成测你自己的意图

坦白说，现在还不够通用，需要改两个地方：

1. `eval/datasets/intent-dataset.jsonl` -- 换成你的 case，schema 就这两行：

```json
{"case_id":"...","input":"...","expected_intent":"record","category":"...","risk":"normal","views":["discovery"]}
{"case_id":"...","input":"...","inject_fault":"llm_timeout","expected_error_code":"llm_timeout","category":"fault","views":["discovery"]}
```

2. `eval/run_intent_eval.py` 顶部的 `LABELS` 和 `HIGH_RISK_REJECT_CATEGORIES` -- 目前标签是硬编码的。

**把这两个搬进配置文件，是下一个迭代的既定目标**，做完才配叫框架。在那之前，这里更像一份可以照抄的参考实现。

---

## 当前范围

```
一句话 → 意图路由 → ┬─ record → 字段抽取 → 三态判定 → 受控写入
                    └─ query  → 查询计划 → 执行 → 结果

训练计划（离线评测入口）：需求解析 planner → 动作库 tool → 计划 generator（固定三步编排）
```

这是**带完整评测闭环的多节点 LLM workflow**，不是自主 Agent，也不是 ReAct--不做概念包装。多轮对话、上下文记忆等未实现能力与后续计划见 [ROADMAP](ROADMAP.md)。

## 关于报告里的 100%

意图识别 locked 集 16 条连续三轮全过，macro-F1 1.0。但这个数字只说明：**这 16 道已定义的题，在这个版本快照上全答对了**。

它不代表真实用户分布下的准确率。原因写在 [数据集设计方法](eval/methodology/意图识别数据集设计方法.md)：意图只有 3 类、全部合成数据、且调优期看过 locked 的失败结果--它是迭代验收集，不是独立盲测集。

小数据集上的高分，信息量低于大数据集上的中等分。

## 文档

- [评测总入口](eval/README.md)：先读这个，再选数据集、Runner 或报告
- [产品总览](docs/product-overview.md) · [总体计划](docs/master-plan.md) · [Phase 1 需求](docs/prd.md)
- 各迭代 PRD：[意图识别](docs/prd-iter1-intent.md) · [抽取写入](docs/prd-iter2-extract.md) · [查询](docs/prd-iter3-query.md) · [Tool Use](docs/prd-iter4-tooluse.md) · [SQLite 持久化](docs/prd-v6-persistence.md)
- [代码实现讲解](docs/迭代一代码实现讲解.md)：每个文件为什么这么写
- [断言方法论](eval/methodology/断言方法论.md)：四问、四态、三级漏斗
- [数据集方法论](eval/methodology/数据集方法论.md)：六步、Golden 准入、Bad Case 回流、脱敏
- [版本演进与问题复盘](eval/reports/版本演进与问题复盘.md)：每个版本发现了什么、怎么改的
- [Iteration 1 正式报告](eval/reports/迭代一意图识别评测报告.md)

## 免责声明

本项目用于 Agent 测试方法研究与展示，不构成训练、医疗或康复建议。
