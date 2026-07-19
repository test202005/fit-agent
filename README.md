# fit-agent

一个以对话式健身记录为业务载体、以可评测和可追溯为核心的 Agent 测试参考项目。

当前恢复入口：[当前进度](当前进度.md)。

Phase 1 / Iteration 1 已完成：实现 `record / query / reject` 三分类意图路由，并交付数据集、Trace、Stub 回归和真实模型评测。测试总入口见 [AI 评测入口](eval/README.md)，新一轮从[意图识别评测计划](eval/意图识别评测计划.md)开始 Review，验收结果见 [Iteration 1 正式报告](eval/reports/迭代一意图识别评测报告.md)。

## 为什么做

- 消费者侧：验证一句自然语言能否降低训练记录和历史查询的操作成本。
- AI 测试侧：验证如何让不确定的模型行为可测、可追溯、可复跑、可回归。

详细定位、目标人群和传播方向见 [产品总览](docs/product-overview.md)。

## 当前范围

Iteration 1 只包含：

- DeepSeek V4 意图分类；
- 严格 JSON 解析；
- Router 级结构化 Trace；
- Stub 正常/故障回归；
- discovery、locked、regression 三视图评测；
- 混淆矩阵、分类别 Precision/Recall、macro-F1 和端到端通过率。

不包含 extractor、训练记录写入、查询执行、HTTP、多轮状态或前端。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

真实模型评测需要项目根目录 `.env`：

```dotenv
DEEPSEEK_API_KEY="your-key"
DEEPSEEK_MODEL="deepseek-v4-flash"
```

`.env`、Trace 和评测结果均被 Git 忽略。

## 验证

```bash
.venv/bin/python -m pytest -q
.venv/bin/python eval/run_intent_eval.py --views all --run-mode stub
.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode live --runs 3
```

评测结果写入 `eval/results/`，Trace 写入 `backend/logs/trace.jsonl`。

## 文档导航

- [产品总览](docs/product-overview.md)：为什么做、为谁做、核心卖点
- [总体计划](docs/master-plan.md)：Phase 与 Iteration 路线
- [Phase 1 总需求](docs/prd.md)
- [Iteration 1 PRD](docs/prd-iter1-intent.md)：业务口径唯一事实源
- [Iteration 1 Architecture](docs/architecture-iter1.md)：当前技术设计
- [AI 评测入口](eval/README.md)：测试范围、数据集、命令、报告和修改规则
- [意图识别评测计划](eval/意图识别评测计划.md)：范围、数据集、字段、数量、执行、回归、归因和通过标准
- [Iteration 1 正式报告](eval/reports/迭代一意图识别评测报告.md)：退出门槛、评测结果和已修复问题
- [意图识别数据集方法与公司对比](eval/methodology/意图识别数据集设计方法.md)：解释当前 100% 的边界、黄金集来源和下一版数据集设计

## 免责声明

本项目用于 Agent 测试方法研究与展示，不构成训练、医疗或康复建议。
