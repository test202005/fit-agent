# fit-agent

## 项目定位

可测试的健身 Agent 参考实现。消费者侧验证“用一句自然语言低摩擦记录和查询训练”的产品价值；AI 测试侧从第一条能力开始内建 trace、测试钩子和评测集，保证自然语言入口不会以错误记录和不可追溯为代价。业务全貌与目标人群见 [docs/product-overview.md](docs/product-overview.md)。

双重目标：
1. 复演/验证 Agent 测试方法论（意图路由评测、多轮测试、断言引擎），反哺公司 agent-native 项目
2. 公开作品：小红书内容 + GitHub 作品集

与 `../project1-suite` 的关系：那是前身，已归档不再迭代。其 `backend/main.py` 的 `run_once()` function calling 写法可作参考。

## 当前阶段

**Phase 1（拆三迭代）· iter-1 意图识别：实现完成，实现 review 判定暂不能关账**（当前恢复入口见 [当前进度.md](当前进度.md)；先处理 [docs/iter1-implementation-review.md](docs/iter1-implementation-review.md) 的 Review 问题，关账门禁六条全勾后才算通过退出门禁；测试入口见 [eval/README.md](eval/README.md)，新一轮计划见 [eval/意图识别评测计划.md](eval/意图识别评测计划.md)，结果见 [eval/reports/迭代一意图识别评测报告.md](eval/reports/迭代一意图识别评测报告.md)，架构见 [docs/architecture-iter1.md](docs/architecture-iter1.md)，PRD 见 [docs/prd-iter1-intent.md](docs/prd-iter1-intent.md) v3）。SDK 使用 OpenAI Python SDK 兼容 DeepSeek API，模型默认 `deepseek-v4-flash`。总纲见 [docs/master-plan.md](docs/master-plan.md)（v3），Phase 1 总需求见 [docs/prd.md](docs/prd.md)（v2）。

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
├── eval/              # AI 评测总入口：方法、数据集、Runner、正式报告和原始结果
└── seed_data/         # 种子画像数据（JSONL）
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
- **Prompt 版本证据**：每次修改 Prompt 必须单独 commit；正式评测报告同时记录 Git commit 与 prompt hash
- **locked 使用规则**：调优期间只运行 discovery / regression；locked 仅在候选版本冻结后一次性运行 3 轮。已暴露的 locked Case 转入 regression，补位 Case 由未参与当前 Prompt 调优的人盲写

## 技术栈

Python + OpenAI Python SDK（DeepSeek API，模型经 `DEEPSEEK_MODEL` 配置）/ JSONL 文件存储 / 不引入 Agent 框架 / Flask 到 iter-3 端到端时引入

## 验证

Iteration 1 验证命令：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python eval/run_intent_eval.py --views all --run-mode stub
.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode live --runs 3
```
