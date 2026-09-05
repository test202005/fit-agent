# Tool Use 设计与测试手册

> 用途：把一个业务能力设计成模型可调用的工具，并完成第一轮测试。范围只到单轮 Tool Use，不包含 ReAct、多轮恢复和 MCP。

## 先判断要不要交给模型

```text
路径固定、可穷举、风险高 → 优先代码路由
需要组合多个能力、根据语境动态选择 → 考虑 Tool Use
```

Tool Use 不是取消代码控制，而是重新分工：

```text
模型：理解需求、选择工具、生成参数
Schema：向模型描述工具契约
Validator：拒绝非法或无依据的参数
Executor：执行确定性业务逻辑
Guardrail：控制权限、副作用、幂等和调用上限
```

## 第一步：从业务任务拆工具

先写用户要完成的任务，再决定工具，不从现有接口机械包装。

每个候选工具回答六问：

1. 解决什么用户任务？
2. 什么时候应该调用？
3. 什么时候不能调用？
4. 最小必填参数是什么？
5. 成功和失败返回什么信息？
6. 是否会写入、删除、通知或产生费用？

工具之间必须职责明确。人无法判断该选哪个工具时，模型也无法稳定选择。Anthropic 同样建议减少功能重叠，并通过真实任务评测工具名、描述和参数设计。[Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)

## 第二步：设计 Schema

```json
{
  "type": "function",
  "function": {
    "name": "count_exercise",
    "description": "统计某个具体动作在日期范围内训练的次数；不用于按身体部位统计。",
    "parameters": {
      "type": "object",
      "properties": {
        "exercise": {"type": "string", "description": "具体动作名"},
        "from": {"type": "string", "description": "YYYY-MM-DD"},
        "to": {"type": "string", "description": "YYYY-MM-DD"}
      },
      "required": ["exercise", "from", "to"],
      "additionalProperties": false
    }
  }
}
```

检查六项：

- `name` 唯一、稳定，能看出动作和对象；
- `description` 同时写清用途与相邻禁用边界；
- 参数名带业务语义和单位，避免 `data/value/user`；
- `required` 只包含完成任务真正必需的字段；
- 类型、枚举、范围和格式能结构化就不只写自然语言；
- Schema 与 Validator 接受的字段完全一致。

若模型接口支持 strict schema adherence，可以启用严格模式；无论是否 strict，服务端 Validator 都不能省略。[OpenAI Function Calling API](https://platform.openai.com/docs/api-reference/chat/create)

## 第三步：先测确定性代码

不用模型，先完成三组单测：

### Schema 契约

- 工具名不重复；
- required 字段都存在于 properties；
- 未知字段是否被禁止；
- Schema 类型与 Validator 一致；
- 每个工具都有对应 Executor。

### Validator 边界

- 缺必填字段、空值、错误类型；
- 零和负数、非法日期、反向范围；
- 多余字段和未知工具；
- 用户没提供的参数不能由系统补猜。

### Executor 与副作用

- 写入数量准确；
- 查询零写入；
- 失败无脏数据；
- 重复请求满足幂等；
- 用户作用域不串数据。

这三组属于代码契约，准出要求 100%。

## 第四步：设计模型评测集

使用传统等价类，不穷举句子：

```text
用户任务 × 工具行为 × 参数风险
```

| 维度 | 代表等价类 |
|---|---|
| 用户任务 | 记录、查询、统计、复合任务、闲聊、超出能力 |
| 工具行为 | 应调用、不应调用、单工具、多工具、漏调、多调、选错 |
| 参数风险 | 完整、缺可选、缺必填、数字干扰、单位、时间、非法类型 |

普通格子先放1条，高风险和历史失败格子放2～3条。正反边界必须成对，例如：

```text
“这周卧推了几次” → count_exercise
“这周练了几次腿” → 不调用工具
```

## 第五步：按五层断言

```text
1. 选没选对：tool name / 应调不调 / 不应调却调
2. 参数对不对：字段、值、单位、时间、无参数幻觉
3. 过程对不对：次数、顺序、无多余调用、首个分歧步骤
4. 执行对不对：工具结果、最终状态、副作用
5. 证据对不对：Trace 与真实调用一致、版本可追溯
```

单轮且只有一种合法路径时，可以精确断言工具序列；存在多种合法路径时，应优先断言最终状态和必要工具，避免把实现策略写死。Anthropic 也建议不要因格式或合法替代路径设置过度严格的 Grader。

报告至少单列：Case 通过率、参数幻觉、不该调用却调用、写入违规、工具错误、首个分歧步骤、`pass^k`、耗时和 Token。

## 第六步：失败归因与回流

```text
失败
→ Schema边界 / Prompt / 模型选择 / 参数 / Validator
  / Executor / Fixture / 断言 / 环境
→ 确认根因
→ 原Case进入Regression
→ 补一个相邻等价类
```

不要一步归因成“模型不会调用工具”。Anthropic 曾通过修改 Web Search 工具描述，修复模型向查询参数多加年份的问题；工具说明本身也是被测对象。

## fit-agent 当前 Review

| 层 | 当前状态 | 结论 |
|---|---|---|
| 工具边界 | 3个工具职责基本清楚 | 已有正反边界，但来源标注较粗 |
| Schema | 有 name/description/properties/required | 缺静态契约测试和显式 schema hash；未启用 strict |
| Validator | 类型、正数、字段、日期和范围校验 | 已覆盖主要边界 |
| Executor | 复用既有写入和查询逻辑 | 已覆盖写入、查询和错误捕获 |
| 模型评测 | 28条 Case，覆盖选择、参数、复合与不调用 | 五层断言已有主要证据，Schema自身除外 |
| Trace | 记录工具调用、结果和首个失败步骤 | 已完成单轮追溯 |
| 失败恢复 | 工具结果未回灌模型 | 尚未验证重试、换工具和最终失败说明 |

当前最小实施项只有两项：补 `TOOL_SCHEMAS` 静态契约测试；核对 Schema 与 Validator 的字段、类型和未知字段策略。当前工具少且 Schema 随代码提交，先用 Git commit 追溯；等工具定义独立配置或多人频繁修改后，再增加 `tool_schema_hash`。工具失败回灌属于下一阶段 Agent Loop，不塞进本轮。

## 准出与收口

- Schema、Validator、Executor 单测 100%；
- 参数幻觉、不该调用却调用、写入违规均为 0；
- 高风险 Regression 每条每轮通过；
- 任一失败能定位到具体层和首个分歧步骤；
- 报告绑定模型、Prompt、Schema、数据集和 Trace 版本；
- 没有新的失败机制或业务能力时停止扩充。

达到以上条件，单轮 Tool Use 即可收口。不要为了显得更像 Agent，提前增加多步循环、MCP、权限平台或几十个无真实需求的工具。
