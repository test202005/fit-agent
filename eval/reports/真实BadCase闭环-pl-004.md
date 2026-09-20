# 真实 Bad Case 闭环：pl-004 复制动作充数

> 记录时间：2026-09-16 09:50（Asia/Shanghai）
> 目的：验证一个已有真实缺陷是否能按“复现 → 断言 → Trace → 回归”闭环。

## 1. Case 与历史问题

| 项目 | 内容 |
|---|---|
| Case | `pl-004` |
| 输入 | 背部、进阶、40 分钟训练计划；动作库该格只有 1 条动作 |
| 历史缺陷 | v1 生成器把同一动作复制成 4 行来凑时长 |
| 历史证据 | [问题清单 P-014](问题清单.md)；v1 live 报告 `plan-report-20260915T163912Z.md` |
| 根因 | Prompt 允许“重复同一个动作的多组”，断言只检查动作来自观察，未限制计划条数 |

## 2. 当前回归结果

本次使用 Stub 替代模型响应执行链路，加载的 Prompt 为 v2，零 Token。响应由代码预先构造，Stub 不执行 Prompt 指令：

| 结果 | 值 |
|---|---:|
| Verdict | `PASS` |
| 黑盒断言 | 6 / 6 |
| 白盒断言 | 5 / 5 |
| 工具返回动作数 | 1 |
| 计划动作数 | 1 |
| 计划动作 | `单臂哑铃划船` |
| Trace 事件数 | 11 |
| Trace ID | `p-pl-004-043a6328-e9b5-456a-9d96-a148d168f4cc` |

查看链路：

```bash
.venv/bin/python eval/trace_view.py p-pl-004-043a6328-e9b5-456a-9d96-a148d168f4cc
```

关键证据是：`tool_result.count = 1`，`generator.result.action_count = 1`，并且最终有 `evaluation.verdict = PASS`。这证明当前 Stub 回归没有再复制条目；Trace 也能看到工具入参、观察结果和评测结论。

## 3. 当前断言下的真实模型复验

2026-09-16 11:20，执行 `.venv/bin/python eval/run_plan_eval.py --views discovery --run-mode live --runs 1`。模型 `deepseek-v4-flash`，temperature 0，planner v1，generator v2。8 条输入为 7 PASS / 0 FAIL / 1 REVIEW / 0 ERROR；可判 7 条共 98/98 断言通过，8 条输入总计 8,104 Token。

报告：[本轮 Live](../results/plan-report-20260916T032017Z.md)；原始结果：[JSONL](../results/plan-results-20260916T032017Z.jsonl)；源码快照：[snapshot](../results/plan-source-20260916T032017Z.json)。源码 hash 为 `sha256:e8f94a947b67363d759ffa4e0f9babb3018de80f1e2a378ca4ad8fd5336f9568`；工作区 dirty，HEAD 不能单独复现本次实现。以上自动产物被 Git 忽略，需随本地证据一起保留。

`pl-004` 原始输入：“我是进阶，来个 40 分钟背部训练”。工具返回 1 条 `单臂哑铃划船`，生成器真实返回：

```json
{"total_min":40,"plan":[{"name":"单臂哑铃划船","sets":4,"reps":8,"rest_sec":60}],"note":"动作库仅返回1个匹配动作，已通过增加组数将总时长补足至约40分钟。"}
```

本 Case 14/14 断言通过，耗用 1,034 Token；Trace ID：`p-pl-004-926da9d7-d723-4864-be5a-3ed3d052d894`，已核对落盘的 11 个事件。该结果支持“本次未再复制动作”，不是 v1/v2 同条件重复对照或稳定性结论。

保留一个实际评测缺口：4 组 × 8 次、组间休息 60 秒，并未提供足够信息证明训练耗时 40 分钟。模型 note 声称“补足时长”，当前断言仅检查 `total_min` 声明值，未验证该解释。动作速度、单组耗时和休息计法尚需定义，后续再决定时长一致性断言。

`pl-006`“练腿，不要深蹲”仍为 observation / REVIEW，虽本次返回空计划，也不算新增排除能力已验收。

## 4. 这次闭环证明了什么

- `plan_actions_unique` 和 `plan_size_within_observation` 能覆盖历史缺陷形状。
- 本次正常 Stub 验证链路与断言可以通过；历史缺陷形状的检出能力由反例单测验证。Prompt v2 的行为效果需引用真实模型运行，不能从 Stub PASS 推出。
- 评测报告、Trace 和源码快照可以串起来，后续可按同一格式记录真实 Bad Case。

## 5. 这次没有证明什么

- Stub 只证明确定性断言能在该形状下工作，不证明真实模型永远不会复制动作。
- 当前检查的是计划条目数量与动作名，不是按 sets/reps 重算真实训练时长。
- 这不是自主 ReAct 评测；当前链路仍是固定的 `planner → tool → generator` 编排。

## 6. 后续复用模板

每个真实 Bad Case 只需补齐五项：

1. 原始输入和历史错误输出；
2. 最小可复现 Fixture / Stub；
3. 失败断言及对应 Trace 步骤；
4. 修复后的同 Case 回归结果；
5. “本次证据不能证明什么”。
