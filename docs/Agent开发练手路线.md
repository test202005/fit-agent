# Agent 开发练手路线

> 用途：主人在本项目里亲手练 Agent 开发的操作路线。先读 [迭代一代码实现讲解](迭代一代码实现讲解.md) 捋懂实现，再按本文动手。
> 本文是练习的**唯一事实源**；知识库笔记（`~/my/weimi/notes/2026-07-19-agent-dev-practice-route.md`）只留方法论判断和指针。
> 纪律：练习一律在 `practice/` 分支做；`.env` 的 key 共用；练习产物不合入 main。

---

## 路径逻辑

沿已实现的手写链路逐层亲手摸：模型抽象 → 固定决策节点 → Trace → 评测 harness → 单轮 Tool Use → 后续多步和多轮状态。前五层项目已有真实实现，当前练习以读通、故障注入和小幅修改为主；多步 Agent Loop 和多轮 Memory 等正式需求确立后再练。

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

### ☐ 练习 3：读通并改动单轮 Tool Use（1～2 小时）

项目已在 `backend/agent.py` 实现单轮 Tool Use：模型选择工具与参数，代码执行工具，并记录 trajectory。

1. 顺着 `backend/agent.py` 定位模型决策、工具执行、错误记录和返回结构
2. 对照 `backend/tools.py` 说清 schema、参数校验和 executor 的分工
3. 对照 `eval/run_tool_eval.py` 找到工具、参数、次数、副作用和首个分歧步骤的断言
4. 在 practice 分支新增一个“未知工具”故障用例，不改主链路功能

**验证**：Tool Use Stub 回归全绿；新故障用例能让对应断言稳定失败；能说清当前链路为什么是 Tool Use，但还不是“工具结果回灌后继续决策”的多步 Agent Loop。

### ☐ 练习 4：读通抽取、查询与受控副作用（2 小时）

1. 跟一遍 `pipeline.py` 的 record 和 query 两条路径
2. 对照 `extractor.py` 说清 `complete / incomplete / invalid` 与是否写入的关系
3. 对照 `query.py` 说清 Planner 与 Executor 为什么分开
4. 从现有单测中各挑一个“回答看起来合理，但不应写入或查询”的反例

**验证**：能用一个 Trace 说明错误是出在路由、抽取/规划还是纯代码执行层；能说出至少两个必须用确定性代码保护、不能只信模型文本的副作用。

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
