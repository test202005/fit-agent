"""trace 契约：声明链路必须走过的节点序列，用一条通用断言校验。

为什么要有它：四条老链路的 Runner 各自手抄一份 `assert_trace_consistency`，
抄的时候漏一个节点没人会发现。契约把「链路应该长什么样」抽成声明，
断言只写一次，新增链路加一份声明即可。

判定口径：契约是**子序列**，不是全等——链路可以多记事件，不能少走声明过的节点。
顺序按声明来，第一个没走过的步骤就是分歧点。
"""

from __future__ import annotations

from typing import Any


# 每条链路：步骤按执行顺序排列；require 是该步骤 payload 必须有的字段
CONTRACTS: dict[str, list[dict[str, Any]]] = {
    "workout_plan": [
        {"node": "planner", "event": "request", "require": ["text"]},
        {"node": "planner", "event": "plan_request",
         "require": ["model", "prompt_name", "prompt_version", "prompt_hash"]},
        {"node": "planner", "event": "plan_response", "require": ["raw_text", "duration_ms"]},
        {"node": "planner", "event": "parse_result", "require": ["ok"]},
        # 工具入参是这条链路唯一的一等断言对象：它缺了，白盒就没得看
        {"node": "tool", "event": "tool_call", "require": ["tool", "arguments"]},
        {"node": "tool", "event": "tool_result", "require": ["count", "names", "reason"]},
        {"node": "generator", "event": "generate_request",
         "require": ["model", "prompt_name", "prompt_version", "prompt_hash"]},
        {"node": "generator", "event": "generate_response", "require": ["raw_text", "duration_ms"]},
        {"node": "generator", "event": "parse_result", "require": ["ok"]},
    ],
}


def _step_label(step: dict[str, Any]) -> str:
    return f"{step['node']}/{step['event']}"


def assert_trace_contract(events: list[dict[str, Any]], contract: str, expected_trace_id: str | None = None) -> dict[str, Any]:
    """通用契约断言。返回 checks 与第一个缺失步骤，供报告直接引用。"""
    steps = CONTRACTS[contract]
    trace_ids = {event.get("trace_id") for event in events}

    cursor = 0
    missing: list[str] = []
    missing_fields: list[str] = []
    invalid_fields: list[str] = []
    for step in steps:
        index = None
        for i in range(cursor, len(events)):
            event = events[i]
            if event.get("node") == step["node"] and event.get("event") == step["event"]:
                index = i
                break
        if index is None:
            missing.append(_step_label(step))
            continue
        cursor = index + 1
        payload = events[index].get("payload")
        payload = payload if isinstance(payload, dict) else {}
        for field in step["require"]:
            if field not in payload:
                missing_fields.append(f"{_step_label(step)}.{field}")
            else:
                value = payload[field]
                valid = value is not None
                if field in {"text", "model", "prompt_name", "prompt_version", "prompt_hash", "tool", "reason"}:
                    valid = isinstance(value, str) and bool(value.strip())
                elif field == "arguments":
                    valid = (isinstance(value, dict)
                             and all(isinstance(value.get(k), str) and value[k].strip()
                                     for k in ("muscle", "difficulty")))
                elif field == "ok":
                    valid = isinstance(value, bool)
                elif field == "count":
                    valid = type(value) is int and value >= 0
                elif field == "duration_ms":
                    valid = type(value) in (int, float) and value >= 0
                elif field == "names":
                    valid = isinstance(value, list) and all(isinstance(v, str) and v for v in value)
                elif field == "raw_text":
                    valid = isinstance(value, str)
                if not valid:
                    invalid_fields.append(f"{_step_label(step)}.{field}")

    checks = [
        {"name": "single_trace_id", "pass": bool(events) and len(trace_ids) == 1
         and all(isinstance(v, str) and bool(v.strip()) for v in trace_ids)
         and (expected_trace_id is None or trace_ids == {expected_trace_id})},
        {"name": "trace_contract_sequence", "pass": not missing},
        {"name": "trace_contract_fields", "pass": not missing_fields and not invalid_fields},
    ]
    return {
        "checks": checks,
        "missing_steps": missing,
        "missing_fields": missing_fields,
        "invalid_fields": invalid_fields,
        "first_missing_step": missing[0] if missing else None,
    }
