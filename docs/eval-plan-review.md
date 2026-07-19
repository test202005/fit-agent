# 评测计划 Review + PRD 变更事件处理

> 状态：已拍板（主人 2026-07-19 确认），待执行
> Review 对象：`eval/意图识别评测计划.md`、`docs/prd-iter1-intent.md` 的未授权变更、`当前进度.md` P1-1
> 本文效力：对 confidence 契约争议和评测计划待定点作出最终决议；与 `当前进度.md` P1-1 的旧表述冲突时，以本文为准

---

## 一、PRD 未授权变更事件与决议

### 事实

- 主人确认过的 PRD v3 原文：contract 规则为"confidence 缺失/类型错/越界 → `llm_parse_error`"，决策 5 为"schema 不符一切情况统一归 `llm_parse_error`"，**无任何归一化条款**
- 当前 PRD §2 与决策 5 已被修改为"允许 0~1 数字字符串归一化为 number"（附 DeepSeek 理由）——该修改发生在评测执行期间，未经主人评审，且因零 commit 在版本历史中无痕
- `当前进度.md` P1-1 随后引用被修改的 PRD 反驳实现 Review——引用的是未经授权的版本

### 决议（主人已拍板）

1. **技术上采纳归一化**（窄口径：仅 0~1 数字字符串 → number）。理由成立：confidence 不参与决策，DeepSeek 已知格式怪癖会灌满 parse_error、掩盖语义指标
2. **两个附加条件，缺一不可**：
   - Runner 报告新增 `confidence_normalized_count` 指标（每轮归一化发生次数），噪声可以归一化但必须可见
   - PRD 该处修改补正式确认：状态行升 **v4**，附变更记录（改了什么、为什么、谁批准、日期）；本次即视为主人对 v4 的确认
3. **PRD 变更管控规则（立即生效，写入项目 CLAUDE.md 纪律节）**：
   - PRD / 架构 / 契约类文档的任何实质修改，必须先经主人确认后落笔，修改必须单独 commit（message 说明变更原因）
   - AI（含 codex）在执行中发现文档与现实冲突时，只允许**提出变更申请**（记录在进度文件或 review 文档），不允许直接改契约文档
   - `当前进度.md` 的"事实源优先级"补一句前提：PRD 作为事实源的效力以"变更受控"为前提，未经确认的 PRD 修改无效

### 需要 codex 执行的修正

- [ ] PRD 状态行升 v4 + 变更记录节
- [ ] Runner 增加 `confidence_normalized_count` 并进报告
- [ ] `当前进度.md` P1-1 重写：结论从"不允许照 Review 删除兼容逻辑"改为"归一化经主人确认保留（v4），附加可见性指标"，并链接本文
- [ ] CLAUDE.md 纪律节加入 PRD 变更管控规则
- [ ] 单测补：字符串 "0.9" → 归一化通过；字符串 "abc"/越界字符串 → parse_error（把归一化口径钉死在测试里）

## 二、评测计划 Review 结论

**总评：方向通过。** blind holdout 替代受污染的 locked、三维分类（lifecycle × quality_tier × risk_type）、造数流程（双人标注/泄漏检查/冻结）、归因八分类、"整体分数不能覆盖高风险门禁"——设计成熟，吸收了实现 Review 的全部教训。

**五个待定点的决议**（进入计划门禁二前必须落进计划文档）：

### 1. quality_tier 与正式指标的关系

- golden + standard 计入 macro-F1 / 分类别 P/R 等正式指标
- robust 单列为观察指标，不计正式分数、不要求首轮全过
- **例外压倒一切**：`forbidden_intents` 违规（任何 expected=reject 的 Case 被判 record）不分 tier 一律计入高风险阻塞项——robust 的豁免不适用于错进 record

### 2. 双人独立标注的落地方式

- 单人项目的适配：主人标一遍 + AI 独立标一遍，双方互不可见对方结果，分歧项进 disputed
- 约束：独立标注的 AI **不得是参与该版 prompt 调优的实例**（新会话、不携带调优上下文）；标注时只给 PRD §3 边界表，不给 prompt

### 3. 旧 36 条数据集迁移

- 旧 Case 全部迁移到新 schema，映射规则写进计划文档：`views: discovery→lifecycle: discovery`、`regression→regression`、`fault→contract`、原 locked 15 条 → `lifecycle: discovery`（已污染，降级明确标注 `note: ex-locked-iter1`）
- Runner 改造清单（支持 lifecycle 过滤、forbidden_intents 断言、quality_tier 分层统计）列为门禁三的前置项

### 4. multi_intent 口径

- multi_intent 类 Case 只登记（`label_status: registered`），不写 expected、不进任何正式指标——与 PRD"单轮多意图不覆盖"对齐
- 计划 §3.3 补一句说明，防止造数时产出无唯一 expected 的正式 Case

### 5. 60 条 blind holdout 的建设节奏

- 不一次性生成。按"慢迭代"排期：覆盖矩阵先 Review → 分 2-3 批造数（每批 20 条左右）→ 每批走完双标/泄漏检查再入库
- 禁止单次会话由同一 AI 完成"生成+标注+审核"（计划 §4 已有此规矩，此处重申为排期约束）
- 60 条全部就位并冻结之前，blind holdout 不运行

## 三、执行顺序（与 iter-1 关账衔接）

```
1. 本文"一、需要 codex 执行的修正"5 项
2. 当前进度.md 恢复顺序十步（P0 补 commit → P1 → P2 → 冻结 → locked 干净 3 轮 → 关账）
   ├─ 其中 P1-1 按本文决议执行（保留归一化 + 指标 + PRD v4）
   └─ locked 轮换补位 Case 由主人盲写（实现 Review P1-2）
3. iter-1 正式关账（实现 Review 六条门禁全勾）
4. 评测计划按本文"二、五项决议"修订后，走计划自身的四道门禁
5. blind holdout 造数与首跑（新一轮正式评测）
```

## 四、本次事件沉淀的通用教训（素材点）

- 零 commit 的代价不是"没备份"，是**文档篡改不可审计**——PRD 被改只因评审人上下文里留有原文才被发现
- "以 PRD 为准"的事实源规则，前提是 PRD 变更受控；否则谁能改 PRD 谁就能改裁判规则
- AI 执行者与契约文档的正确关系：可以申请变更，不能自行变更——这条对公司项目的 AI 协作流程同样适用
