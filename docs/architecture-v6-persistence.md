# V6 技术设计：SQLite 持久化与数据一致性

> 对应 PRD：[V6 SQLite 持久化与数据一致性](prd-v6-persistence.md)  
> 状态：设计完成，待实现  
> 目标：在保持一条命令可运行的前提下，为现有 Storage 抽象增加真实持久化能力。

## 1. 设计原则

- 不改变当前训练记录业务协议；
- 不让上层业务依赖 SQLite API；
- FakeStorage 继续用于单元测试，JsonlStorage 继续用于教学和对照；
- SQLite 是默认完整运行实现；
- 只实现当前真实需要的持久化、隔离、事务、重复请求和恢复，不提前建设数据库平台。

## 2. 依赖关系

```text
HTTP / Tool Use / Pipeline / Query
                ↓
        StorageClient Protocol
          ↙        ↓        ↘
    FakeStorage  JsonlStorage  SQLiteStorage
```

上层只依赖 `StorageClient`。数据库切换不应改变模型 Prompt、Extractor、Query Planner 或 Tool schema。

## 3. 请求上下文

当前请求只有 `text`，V6 增加两个上下文字段：

```json
{
  "user_id": "user-a",
  "request_id": "req-001",
  "text": "今天卧推做了四组"
}
```

- `user_id`：数据隔离的最小标识；缺省使用 `demo-user`，保证新人可以直接运行；
- `request_id`：重试幂等的技术标识；缺省由服务端生成；
- `trace_id`：仍然只负责一次执行轨迹追踪，不作为幂等键或业务主键。

`user_id` 由 HTTP 请求上下文传给 Pipeline，不进入模型 Prompt，也不允许模型通过 Tool 参数生成或修改。本轮使用客户端传入的 `user_id` 验证数据作用域，它不是经过认证的用户身份，因此不能据此宣称系统已完成鉴权和权限控制。

同一用户的同一 `request_id` 重试时不重复写入，返回第一次写入的 `written_ids`，并在业务返回中标记 `idempotent_replay: true`。不同 `request_id` 即使文本相同，也按两次独立请求处理。客户端未提供 `request_id` 时由服务端生成；这类请求再次发送会获得新的 ID，因此不具备跨请求重试幂等能力。

## 4. Storage 接口

当前接口：

```python
append(records, trace_id, now)
read_all()
```

V6 最小扩展：

```python
@dataclass(frozen=True)
class StorageWriteResult:
    written_ids: list[str]
    idempotent_replay: bool


append(
    records,
    trace_id,
    now,
    user_id="demo-user",
    request_id=None,
) -> StorageWriteResult

read_all(user_id: str) -> list[dict]
```

兼容要求：

- FakeStorage 和 JsonlStorage 接受新增上下文，不改变原有测试意图；
- `user_id` 进入每条持久化记录；
- `request_id` 在独立的 `write_requests` 表中用于同一批写入的幂等约束；一条请求可以对应多条训练记录；
- Pipeline 将 `StorageWriteResult` 中的 `written_ids` 和 `idempotent_replay` 原样放入业务返回，不自行推断是否重放；
- `append` 一次接收的多条记录属于同一事务单元；
- 业务侧 `read_all` 必须显式传入 `user_id`，不提供默认全量读取；测试如需检查全库，使用 SQLite 测试辅助方法直接查询；
- Query Executor 必须使用当前用户范围读取，不允许先读全量再由上层过滤；
- 记录 `id` 使用 UUID 生成，不复用业务时间、`trace_id` 或 `request_id`。

## 5. SQLite 最小 Schema

```sql
CREATE TABLE workout_records (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    state TEXT NOT NULL,
    exercise TEXT,
    weight_kg REAL,
    sets INTEGER,
    reps INTEGER,
    duration_min REAL,
    distance_km REAL
);

CREATE TABLE write_requests (
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(user_id, request_id)
);

CREATE INDEX ix_workout_records_user_ts
ON workout_records(user_id, ts);

CREATE INDEX ix_workout_records_user_request
ON workout_records(user_id, request_id);
```

Schema 只复用当前 `RECORD_FIELDS` 和 `state` 协议。`ts` 表示用户训练发生的业务时间，继续受 Frozen Clock 控制；`created_at` 表示数据库实际写入时间，用于追踪和排障。启动时使用 `CREATE TABLE IF NOT EXISTS` 初始化；本轮不实现完整迁移平台。

## 6. 事务与幂等行为

一次 `append` 的处理顺序：

```text
开启事务
→ 在 write_requests 中登记 user_id / request_id
→ 批量写入记录
→ 任意一条失败则回滚全部
→ 成功后提交
```

幂等只针对客户端显式传入的 `request_id`，不根据自然语言文本、时间或动作名称猜测重复请求。若登记发现同一用户的同一请求已处理，则按 `(user_id, request_id)` 读取已提交记录，返回第一次写入的 `written_ids` 和 `idempotent_replay: true`，不再次写入。首次成功写入返回 `idempotent_replay: false`。

SQLite 每次 Storage 操作创建独立连接，设置有限的 `busy_timeout`，在事务提交或回滚后关闭；本轮不引入连接池。数据库锁等待超时或事务异常作为存储错误记录到 Trace，并由 HTTP 统一映射为当前 `internal_error`，不向调用方暴露 SQL 和堆栈。

JsonlStorage 不强行模拟 SQLite 的事务和唯一约束；它作为教学/对照实现保留现有行为，SQLite 专项测试只对 SQLite 的真实能力负责。

## 7. 代码改动边界

预计涉及：

- `backend/storage.py`：接口参数、SQLiteStorage、初始化和事务；
- `backend/pipeline.py`：传递 `user_id` / `request_id`；
- `backend/query.py`、`backend/tools.py`：按用户范围读写；
- `backend/app.py`：解析请求上下文、选择默认 SQLite；
- `tests/`：Storage、HTTP、事务、隔离、恢复和回归测试；
- `README.md` / `eval/README.md`：更新一条命令运行说明。

不改模型 Prompt、评测 Case 语义和 Tool schema，除非接入用户上下文后发现原有契约必须同步调整。

## 8. 测试顺序

1. SQLite 基础写入和读取；
2. Schema 字段、状态和用户范围；
3. 重启恢复；
4. 多用户隔离；
5. 重复 `request_id`；
6. 批量写入中途失败并回滚；
7. Frozen Clock 时间查询；
8. HTTP 端到端；
9. 原有 140 条单测和四套 Stub 回归。

每个新增断言都要有反例：故意破坏隔离、事务或重复约束时，测试必须失败。

## 9. 暂不决策的事项

- 真实生产环境使用 PostgreSQL 还是 MySQL；
- 完整 Schema 迁移方案；
- 高并发连接池和性能指标；
- 复杂权限模型；
- 删除、修改、撤销记录的产品协议。

这些事项不阻塞 SQLite 版本，但不能在本轮被默认为已解决。

## 10. 设计准出

- Storage 接口参数和用户上下文已明确；
- SQLite Schema、索引、事务和幂等边界已明确；
- Fake/JSONL/SQLite 三种实现职责已明确；
- 测试范围和非目标已明确；
- 仍保持一条命令可运行。

达到设计准出后，才进入实现阶段。
