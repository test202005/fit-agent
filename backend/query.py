from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.clock import Clock, LOCAL_TZ
from backend.llm import LLMApiError, LLMClient, LLMTimeout, TEMPERATURE
from backend.storage import StorageClient
from backend.trace import Tracer


PROMPT_PATH = Path(__file__).parent / "prompts" / "query_planner_v1.txt"
QUERY_TYPES = ("list_by_date", "count_by_exercise", "unsupported")


def load_prompt() -> tuple[str, str]:
    content = PROMPT_PATH.read_text(encoding="utf-8")
    return content, "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------- Planner：唯一的不确定环节 ----------


def _is_date(value: Any) -> bool:
    """必须是零填充的 YYYY-MM-DD。范围过滤靠字符串比较，非零填充会比错。"""
    if not isinstance(value, str) or len(value) != 10:
        return False
    if value[4] != "-" or value[7] != "-":
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def parse_plan(raw_text: str) -> dict[str, Any]:
    """严格解析查询计划，结构不符一律拒绝。"""
    try:
        parsed = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(parsed, dict) or "type" not in parsed:
        raise ValueError("missing type")
    plan_type = parsed["type"]
    if plan_type not in QUERY_TYPES:
        raise ValueError("unknown type")

    if plan_type == "unsupported":
        if set(parsed) != {"type"}:
            raise ValueError("unsupported takes no extra fields")
        return {"type": "unsupported"}

    if plan_type == "list_by_date":
        if set(parsed) != {"type", "date"} or not _is_date(parsed["date"]):
            raise ValueError("invalid list_by_date")
        return {"type": "list_by_date", "date": parsed["date"]}

    if set(parsed) != {"type", "exercise", "from", "to"}:
        raise ValueError("invalid count_by_exercise fields")
    exercise = parsed["exercise"]
    if not isinstance(exercise, str) or not exercise.strip():
        raise ValueError("invalid exercise")
    if not _is_date(parsed["from"]) or not _is_date(parsed["to"]):
        raise ValueError("invalid range")
    if parsed["from"] > parsed["to"]:
        raise ValueError("reversed range")
    return {
        "type": "count_by_exercise",
        "exercise": exercise.strip(),
        "from": parsed["from"],
        "to": parsed["to"],
    }


def plan_query(
    text: str, llm: LLMClient, tracer: Tracer, trace_id: str, clock: Clock
) -> dict[str, Any]:
    system_prompt, prompt_hash = load_prompt()
    now = clock.now()
    # 相对日期要靠"今天是几号"才能解析，所以把当前时间显式喂给模型
    user_text = f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（{'一二三四五六日'[now.weekday()]}）\n用户输入：{text}"
    tracer.emit(
        trace_id,
        "plan_request",
        {"model": llm.model, "prompt_hash": prompt_hash, "temperature": TEMPERATURE,
         "now": now.isoformat()},
        node="planner",
    )
    started = time.perf_counter()
    try:
        result = llm.complete(system_prompt, user_text)
    except LLMTimeout:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_timeout"}, node="planner")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_timeout"}
    except LLMApiError:
        tracer.emit(trace_id, "result", {"ok": False, "error_code": "llm_api_error"}, node="planner")
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_api_error"}

    tracer.emit(
        trace_id,
        "plan_response",
        {"raw_text": result.raw_text,
         "duration_ms": round((time.perf_counter() - started) * 1000, 2)},
        node="planner",
    )
    try:
        plan = parse_plan(result.raw_text)
    except ValueError:
        tracer.emit(trace_id, "parse_result", {"ok": False}, node="planner")
        tracer.emit(
            trace_id, "result", {"ok": False, "error_code": "llm_parse_error"}, node="planner"
        )
        return {"ok": False, "trace_id": trace_id, "error_code": "llm_parse_error"}

    tracer.emit(trace_id, "parse_result", {"ok": True, "plan": plan}, node="planner")
    return {"ok": True, "trace_id": trace_id, "plan": plan}


# ---------- Executor：纯代码，完全确定 ----------


def _row_local_date(row: dict[str, Any]) -> str:
    """记录的业务日期按本地时区算，避免 UTC 存储导致跨零点错位。"""
    return datetime.fromisoformat(row["ts"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def _sort_desc(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 口径 5：按写入时间倒序，最新的在前
    return sorted(rows, key=lambda row: row["ts"], reverse=True)


def execute_plan(plan: dict[str, Any], storage: StorageClient) -> dict[str, Any]:
    rows = storage.read_all()
    if plan["type"] == "unsupported":
        return {"records": [], "count": 0, "supported": False}

    if plan["type"] == "list_by_date":
        matched = [row for row in rows if _row_local_date(row) == plan["date"]]
        return {"records": _sort_desc(matched), "count": len(matched), "supported": True}

    matched = [
        row
        for row in rows
        if row.get("exercise") == plan["exercise"]
        and plan["from"] <= _row_local_date(row) <= plan["to"]
    ]
    return {"records": _sort_desc(matched), "count": len(matched), "supported": True}


def week_start(now: datetime) -> str:
    """口径 3 的可执行定义：这周 = 周一起算，不是滚动七天。

    生产链路里日期换算由 Planner 在 Prompt 中完成，这个函数不参与线上调用；
    它的作用是让「周一起算」这条产品口径有一份代码事实源，供评测剧本与单测比对。
    """
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")
