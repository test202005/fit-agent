from __future__ import annotations

from typing import Any, Callable

from backend.clock import Clock
from backend.extractor import QUANT_FIELDS, decide_record_state
from backend.query import execute_plan
from backend.storage import RECORD_FIELDS, StorageClient


class ToolError(Exception):
    """工具执行失败。与模型自身的选择错误分开，便于归因。"""


# ---------- 工具定义（喂给模型的清单）----------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "create_record",
            "description": "记录一次已经发生的训练。只在用户明确陈述已完成的训练时调用；未来计划、疑问、闲聊都不要调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise": {"type": "string", "description": "动作名或训练类型，如 卧推、跑步、胸"},
                    "weight_kg": {"type": "number", "description": "重量，公斤"},
                    "sets": {"type": "number", "description": "组数"},
                    "reps": {"type": "number", "description": "每组次数"},
                    "duration_min": {"type": "number", "description": "时长，分钟"},
                    "distance_km": {"type": "number", "description": "距离，公里"},
                },
                "required": ["exercise"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_records",
            "description": "查询某一天练了什么。date 必须是 YYYY-MM-DD 格式的具体日期，相对日期要先按当前时间换算。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_exercise",
            "description": "统计某个动作在一段日期范围内练了几次。日期必须是 YYYY-MM-DD 格式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "exercise": {"type": "string"},
                    "from": {"type": "string", "description": "YYYY-MM-DD"},
                    "to": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["exercise", "from", "to"],
            },
        },
    },
]

TOOL_NAMES = {schema["function"]["name"] for schema in TOOL_SCHEMAS}


# ---------- 参数校验（复用 iter-2 既有规则，不重写一套）----------


def validate_create_args(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ToolError("args must be an object")
    unknown = set(args) - set(RECORD_FIELDS)
    if unknown:
        raise ToolError(f"unknown fields: {sorted(unknown)}")
    exercise = args.get("exercise")
    if not isinstance(exercise, str) or not exercise.strip():
        raise ToolError("invalid exercise")
    record: dict[str, Any] = {"exercise": exercise.strip()}
    for field in QUANT_FIELDS:
        value = args.get(field)
        if value is None:
            record[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ToolError(f"invalid {field}")
        if value <= 0:
            raise ToolError(f"non-positive {field}")
        record[field] = value
    return record


def _require_date(value: Any, field: str) -> str:
    from backend.query import _is_date

    if not _is_date(value):
        raise ToolError(f"invalid {field}")
    return value


# ---------- 执行 ----------


def make_executors(
    storage: StorageClient, clock: Clock, trace_id: str
) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
    """把现成能力包成工具。业务逻辑一行不改，保证与固定链路可比。

    trace_id 由闭包捕获，不用全局状态——每次请求各自独立。
    """

    def create_record(args: dict[str, Any]) -> dict[str, Any]:
        record = validate_create_args(args)
        record["state"] = decide_record_state(record)
        written = storage.append([record], trace_id, clock.now())
        return {"written": len(written), "ids": written, "state": record["state"]}

    def query_records(args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"date"}:
            raise ToolError("query_records takes only date")
        plan = {"type": "list_by_date", "date": _require_date(args.get("date"), "date")}
        outcome = execute_plan(plan, storage)
        return {"count": outcome["count"], "records": outcome["records"]}

    def count_exercise(args: dict[str, Any]) -> dict[str, Any]:
        if set(args) != {"exercise", "from", "to"}:
            raise ToolError("count_exercise takes exercise, from, to")
        exercise = args.get("exercise")
        if not isinstance(exercise, str) or not exercise.strip():
            raise ToolError("invalid exercise")
        start = _require_date(args.get("from"), "from")
        end = _require_date(args.get("to"), "to")
        if start > end:
            raise ToolError("reversed range")
        plan = {
            "type": "count_by_exercise",
            "exercise": exercise.strip(),
            "from": start,
            "to": end,
        }
        outcome = execute_plan(plan, storage)
        return {"count": outcome["count"]}

    return {
        "create_record": create_record,
        "query_records": query_records,
        "count_exercise": count_exercise,
    }
