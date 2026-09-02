# Agent 开发练手路线

> 用途：主人在本项目里亲手练 Agent 开发的操作路线。先读 [迭代一代码实现讲解](迭代一代码实现讲解.md) 捋懂实现，再按本文动手。
> 本文是练习的**唯一事实源**；知识库笔记（`~/my/weimi/notes/2026-07-19-agent-dev-practice-route.md`）只留方法论判断和指针。
> 纪律：练习一律在 `practice/` 分支做（main 关账中，等 codex 补完 commit 基线再拉分支）；`.env` 的 key 共用；练习产物不合入 main。

---

## 路径逻辑

沿手写链路逐层亲手摸：模型抽象 → 决策节点 → 可观测 → 评测 harness → 工具循环 → 多轮状态。前四层项目里已有实现（读+改），后两层还没有（先自己写，等正式版出来对照）。学开发的直接回报是测试：亲手写过每一层，才知道每一层会怎么坏。

## 练习清单（难度递增，每个带验证标准）

### ☐ 练习 1：读通一条请求（半小时，现在就能做，不用等关账）

1. 跑 `.venv/bin/python eval/run_intent_eval.py --views discovery --run-mode stub`
2. 打开 `backend/logs/trace.jsonl`，挑一个 trace_id
3. 对着 `backend/router.py` 把 input_received → llm_request → llm_response → parse_result → result 五个事件逐行定位到产生它的代码

**验证**：不看代码能说出——bad_request 的 trace 为什么只有两个事件？这个特征怎么用来断言防御路径没调 LLM？

### ☐ 练习 2：加一个故障类型（1 小时，10 行级，需在 practice 分支）

1. 给 `backend/llm.py` 的 `StubLLM` 加 `fault="llm_empty"`（返回空字符串）
2. 在 `tests/test_router_unit.py` 补单测，断言它落 `llm_parse_error`
3. 跑 pytest + stub 回归

**验证**：全绿；能说出空字符串为什么走 parse_error 而不是 api_error（提示：异常发生在哪一层、翻译发生在哪一层）。

### ☐ 练习 3：手写 tool-calling 循环（一个晚上，价值最高）

项目里还没有 FC，这是补上"Agent"区别于"workflow"的关键一层。

1. 独立小脚本（放 scratchpad 或 practice 分支的 `playground/`，不进主链路）
2. 用 OpenAI SDK 的 `tools` 参数定义一个假工具 `get_workout_history`（返回写死的 JSON）
3. while 循环：发消息 → 模型返回 `tool_calls` → 本地执行工具 → 结果以 `role=tool` 消息回灌 → 模型给最终答复
4. 约 30 行，参考 OpenAI Function calling 官方指南

**验证**：能画出"模型选择 → 代码执行 → 结果回灌"的循环图；能说出 Agent 框架的工具回调机制在循环里替你做了哪几步；能指出 FC 特有错误（选错工具/参数幻觉/循环失控）各发生在循环的哪个位置。

### ☐ 练习 4：预写 iter-2 的 extractor（进阶，在 codex 动手前完成才有意义）

1. iter-2 正式内容："自然语言 → 结构化训练记录 + complete/incomplete/invalid 三态判定"（口径见 [prd.md](prd.md) §4-5）
2. 自己先写一版糙的：抽取 prompt + 解析校验 + 三态逻辑，能跑通"今天卧推60kg 4组8次"和"今天练了胸"两个入口即可
3. 等 codex 正式版出来，做 diff

**验证**：diff 时能列出"我没想到的边界"≥2 条 和"正式版的测试盲区"≥2 条，记入下方练习日志。

## 练习日志

> 每个练习完成后在此追加：日期、实际收获、意外/与预期不符的点。这些是内容素材的原料。

（暂无）

## 每个练习的对照收获

| 练习 | 对照收获 |
|------|---------|
| 1 | 结构化 trace vs 散日志的定位效率差 |
| 2 | 故障注入在哪层做才不失真：stub 在 llm 层，router 走真实路径 |
| 3 | 框架藏掉的工具调用循环长什么样 |
| 4 | 抽取关口的坏法与断言设计 |
