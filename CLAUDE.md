# fit-agent

## 项目定位

可测试的健身 Agent 参考实现。消费者侧验证“用一句自然语言低摩擦记录和查询训练”的产品价值；AI 测试侧从第一条能力开始内建 trace、测试钩子和评测集，保证自然语言入口不会以错误记录和不可追溯为代价。业务全貌与目标人群见 [docs/product-overview.md](docs/product-overview.md)。

双重目标：
1. 复演/验证 Agent 测试方法论（意图路由评测、多轮测试、断言引擎），沉淀可复用的评测脚手架
2. 公开作品：小红书内容 + GitHub 作品集

前身是一个已归档的原型项目，不再迭代；本项目推倒新建，仅参考其 function calling 的写法。

## 当前阶段

**V7.0 已实现固定多步训练计划生成与可观测性，尚非自主 ReAct；V5.0 稳定性与成本口径、V5.1 两模型受控对比、V6 SQLite 持久化均已关账。**

权威事实源是 [当前进度.md](当前进度.md)——本节只给稳定坐标，具体进度以那份为准，两边冲突时以 `当前进度.md` 为准。

已实现的迭代：iter-1 意图识别（`1a61947`）、iter-2 抽取与受控写入、iter-3 查询与 Clock 注入、iter-4 工具调用与双架构对比、iter-5 训练计划生成。最新验证入口见 [ROADMAP.md](ROADMAP.md)。

| 入口 | 文件 |
|---|---|
| 恢复工作 | [当前进度.md](当前进度.md) |
| 人机协作参考 | [Agentic Coding 协作指南](docs/Agentic-Coding协作指南.md) |
| 评测总入口 | [eval/README.md](eval/README.md) |
| 迭代规划依据 | [docs/Agent能力与评测全景.md](docs/Agent能力与评测全景.md) |
| 问题细账 | [eval/reports/问题清单.md](eval/reports/问题清单.md) |
| 版本主线 | [eval/reports/版本演进与问题复盘.md](eval/reports/版本演进与问题复盘.md) |
| 内容系列总纲 | [content/系列大纲.md](content/系列大纲.md) |

SDK 使用 OpenAI Python SDK 兼容 DeepSeek API，模型默认 `deepseek-v4-flash`（另有 `deepseek-v4-pro` 可用于多模型对比）。总纲见 [docs/master-plan.md](docs/master-plan.md)（v4），Phase 1 总需求见 [docs/prd.md](docs/prd.md)（v2）。

流程约定：每迭代先写专项 PRD → 主人评审 → 过门禁 → 动码 → 过退出门禁才进下一迭代。iter-1 动码时先 `git init` + `.gitignore`（logs/、数据文件）。

### 连续执行授权

当主人明确说“睡觉”“全自动完成”“不要再确认”或同等意思时，视为对当前已确认迭代范围的连续执行授权：

- AI 自主完成实现、项目本地依赖安装、Git 初始化、测试、真实模型小额评测、代码 Review、修复与复验，不逐项等待确认
- discovery baseline 后可由 AI 制定保守退出阈值并执行 locked 验收，报告必须记录阈值依据
- 遇到单点外部阻塞时继续完成其余安全工作，最终只报告真实阻塞，不停在原地等回复
- 授权不扩大项目范围，不包含公开发布、全局依赖、系统配置、数据库迁移、生产部署或范围外文件删除
- 密钥只从本地 `.env` 加载，不输出、不写入日志、不进入 Git

## 目录结构约定

```
fit-agent/
├── CLAUDE.md          # 本文件：约定与当前阶段
├── docs/              # 产品定位、PRD、架构和迭代计划（master-plan.md 为总纲）
├── backend/           # Flask + Agent 链路代码
├── tests/             # 确定性代码单测
└── eval/              # AI 评测总入口：方法、数据集、Runner、正式报告和原始结果
```

- 代码、变量名和代码型数据文件使用英文；`eval/` 下供人和 AI 查找的 Markdown 文档优先使用可搜索的中文文件名
- 产品和研发设计只进 `docs/`；AI 评测方法、数据集和报告只进 `eval/`
- `eval/reports/` 放人工核对的正式结论并进入版本库；`eval/results/` 放自动生成的原始结果并忽略
- 评测数据集和报告不散落在 backend

## 纪律

- **trace-first**：任何新链路代码从第一行起就带 traceId 结构化日志，不允许 print 调试
- **范围红线**：范围蔓延是本项目头号风险。master-plan 的「非目标」清单里的东西不做；每个 Phase 验收标准中测试产出是硬指标，功能"够测"即停
- **LLM 可替换**：所有 LLM 调用走统一入口，支持 stub 注入，保证回归不花 token
- 密钥走环境变量 `DEEPSEEK_API_KEY`，不进代码不进 commit
- **Prompt 版本证据**：`backend/prompts/manifest.json` 是当前 Prompt 入口；新增版本时保留旧文件并更新 manifest，不原地覆盖旧版本；每次 Prompt 修改必须单独 commit；Trace 与正式评测报告同时记录 prompt name、version、hash 和 Git commit
- **locked 使用规则**：调优期间只运行 discovery / regression；locked 仅在候选版本冻结后一次性运行 3 轮。已暴露的 locked Case 转入 regression，补位 Case 由未参与当前 Prompt 调优的人盲写
- **契约文档变更管控**：PRD、架构和契约类文档的实质修改必须先经主人确认，再单独 commit 并说明变更原因。执行中发现文档与现实冲突时，AI 只能先在进度或 Review 文档提出变更申请，不得直接改写契约

## 技术栈

Python + OpenAI Python SDK（DeepSeek API，模型经 `DEEPSEEK_MODEL` 配置）/ SQLite 存储（V6 起为默认，JsonlStorage 保留为教学对照）/ 不引入 Agent 框架 / Flask 到 iter-3 端到端时引入

## 验证

改完必跑：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python eval/run_intent_eval.py --views all --run-mode stub
.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode live --runs 3
```

P-007 已修：五套 Runner 退出码统一为四态契约——只有 `PASS / REVIEW` 返回 0，出现 `FAIL / ERROR` 返回 1。`run_intent_eval.py --views all` 现在返回 0（PASS 56 / REVIEW 2 / FAIL 0）。

计划 Runner 的故障注入属于检测器自测，独立于上述业务门禁：原始结果保留预期 FAIL，只有失败集合完全命中预期才通过；漏检、额外失败或 ERROR 返回 1。

五套 Runner 共用 `--runs`（多轮）、`--models`（多模型横向对比）和 `--temperature`（覆盖采样温度），后两个仅 live 生效。报告按 `模型 × 轮次` 分节并汇总 pass@k / pass^k / flaky / token，metadata 记实际生效温度。口径定义见 [eval/stability.py](eval/stability.py)，读法见 [eval/README.md](eval/README.md)。

`--temperature` 只用于稳定性专项——温度 0 下零波动是必然结果，证明不了波动检测有效，需要故意升温做对照。升温结果不是质量结论，不得进验收报告。

```bash
.venv/bin/python eval/run_extract_eval.py --views all --run-mode stub --runs 2
.venv/bin/python eval/run_query_eval.py   --views all --run-mode stub --runs 2
.venv/bin/python eval/run_tool_eval.py    --views all --run-mode stub --runs 2
.venv/bin/python eval/run_plan_eval.py    --views all --run-mode stub
```

架构对比（真实模型，会花 token）：

```bash
.venv/bin/python eval/run_architecture_compare.py --runs 2
```

报告含通过率、耗时、调用次数和 token 五个维度，两种架构同口径——都从 trace 事件收集 usage，不读业务返回结构。
