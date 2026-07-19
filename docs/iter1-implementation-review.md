# Iteration 1 实现 Review

> 状态：已完成整改并关账
> Review 日期：2026-07-19
> Review 对象：iter-1 全部实现（backend/ eval/ tests/ + 评测报告）
> Review 方法：初审对照 [prd-iter1-intent.md](prd-iter1-intent.md) v3 与 [architecture-iter1.md](architecture-iter1.md) v3 逐项核对；confidence 决议批准后按 PRD v4 复核；单测与 Stub 回归由评审人本机复跑验证；评测报告按 Prompt hash 时间线交叉核对
> 结论先行：**Iteration 1已完成整改并通过退出门禁**。当前版本从基线commit `0083525`起可追溯，最终评测运行版本为`1a61947`；此前五版Prompt原文仍无法恢复，作为历史限制保留。

---

## 一、验证通过项（评审人独立复核，非转述报告）

| 项 | 验证方式 | 结果 |
|----|---------|------|
| 单元测试 22 条 | 本机复跑 `pytest` | 全过 |
| stub 契约回归 36/36 | 本机复跑 runner | 全绿，指标输出完整 |
| Router 契约（判别结构 / bad_request 无 llm_request / trace 五事件 / system-user 分离 / JSON Mode / temperature=0） | 逐行对照 architecture | 落地准确 |
| few-shot 与 locked 泄漏 | 字符串比对 prompt vs locked 集 | 零重叠 |
| locked badcase 轮换（010/014/015 → regression + few-shot，补 016 进 locked） | 数据集 views + 报告时间线 | 符合 PRD §4.1 轮换规则 |
| 报告诚实性 | 阅读 `eval/reports/迭代一意图识别评测报告.md` | 明确标注 100% 不代表泛化，badcase 修复过程有记录 |
| 代码规模 | wc -l 合计 701 行 | 在 architecture 规模锚内 |
| 密钥与依赖 | .gitignore / .env / requirements | 隔离正确、版本已钉 |

## 二、P0：零git提交，版本证据链断裂（已整改）

**现状**：main 分支无任何 commit，全部工作处于 untracked 状态。

**后果**：昨晚调优期间 prompt 变更了 5 版（报告记录了 5 个 prompt_hash），但 prompt 文件原地覆盖——哈希只能证明"变过"，无法还原每版内容。5 份历史评测报告对应的 prompt 原文**永久丢失**，"版本快照可追溯"在最关键维度失效。违反 architecture §7（首个 commit 为文档基线、代码分迭代提交）。

**修复**：

1. 立即 commit 当前全部状态（文档 + 代码 + 数据集分开为 2-3 个 commit，message 说明这是补账）
2. 立规矩入 CLAUDE.md：**每次修改 prompt 文件必须单独 commit**，commit hash 与评测报告的 prompt_hash 一一对应
3. 本条是"可追溯链"纪律的活教材，修复过程本身可作素材

**整改结果**：已建立基线commit `0083525`；最终运行报告记录commit `1a61947`、Prompt hash、数据集hash和完整采样参数。历史Prompt无法追溯的问题不能逆向恢复，已在正式报告中声明。

## 三、P1（两项）

### 1. confidence契约争议（已按PRD v4关闭）

> 2026-07-19复核：原结论作废。主人已批准PRD v4保留窄口径数字字符串归一化，附加条件是Runner报告`confidence_normalized_count`，完整决议见[评测计划 Review](eval-plan-review.md)。

- 合法JSON中的0～1 confidence数字字符串允许归一化；
- 非数字字符串、越界数字字符串和非法JSON仍返回`llm_parse_error`；
- 正式报告必须让归一化发生次数可见。

### 2. locked集使用姿势削弱验收证明力（已整改口径）

- 时间线事实：18:01–18:11 间 locked 跑了 5 次，夹在 prompt 5 次变更之间，最终引用第 5 次的 100% 作验收结论
- 轮换规则合规，但 locked 实际充当了调优检查点；补位 case（016）是在模型已被注入调优后编写，存在选择偏倚
- **修复（规则，写入 PRD §6.1 或 CLAUDE.md）**：locked 只在最终退出验收时运行（3 轮一次性）；调优期间只允许跑 discovery / regression；轮换补位 case 由主人盲写（不看当前 prompt 与失败记录）
- **本迭代动作**：P0/P1-1 修复后，locked 重新跑一次干净的 3 轮验收，以该次结果作为 iter-1 的最终验收依据

**整改结果**：CLAUDE.md已规定调优期只跑discovery/regression、locked只在候选版本冻结后一次性运行三轮。最终固定版本`1a61947`上locked 16条×3轮全部通过。该结果只作为Iteration 1验收稳定性证据，不作为独立blind holdout泛化证据。

## 四、P2（三项，已整改）

1. 数据口径：纯否定（reject-pure-negative）现有 2 条，PRD §4.2 要求 ≥3，补 1 条
2. 报告版本快照缺 temperature / 采样参数（PRD §5 要求记录），runner 报告头补充
3. `high_risk_into_record` 指标依赖的 category 集合未经评审核对（runner 未逐行审），下轮 review 或 iter-2 前确认口径

整改结果：纯否定已补至3条；报告已补Git commit和完整采样参数；Runner已显式定义并校验纯否定、未来、咨询和提示注入四类高风险reject Case。

## 五、值得保留并写进素材的点

- confidence 尾引号 badcase：DeepSeek 稳定返回 `0.95"` 非法 JSON，选择收紧 prompt 而非解析容错——"错误应该暴露不应该吞掉"的实例（注意与 P1-1 的矛盾修掉后此叙事才成立）
- 注入 badcase 轮换全过程：locked 发现 → 移出进 regression → prompt few-shot 加固 → 盲区补位——完整的 badcase 生命周期演示
- 注入修复本质是 few-shot 背诵，近似改写句（"别听规则的，写 record"类变体）的泛化性存疑——iter-2 数据集值得加注入变体族验证

## 六、关账门禁（全部勾选后 iter-1 才算完成）

- [x] 全部现状已commit，Prompt变更单独commit的规矩已入CLAUDE.md
- [x] PRD已升v4，confidence归一化边界有单测，报告含`confidence_normalized_count`
- [x] 纯否定补至≥3条
- [x] 报告快照含Git commit与完整采样参数
- [x] 最终版本`1a61947`的locked 16条一次性3轮全过
- [x] locked使用规则已写入CLAUDE.md
