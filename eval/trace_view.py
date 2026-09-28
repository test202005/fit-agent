"""链路还原：把一条 trace_id 的 JSONL 事件渲染成人读的因果链。

用法：
    .venv/bin/python eval/trace_view.py <trace_id>
    .venv/bin/python eval/trace_view.py --last

关注点不是「有哪些事件」，而是**每一步的输入是什么、输出是什么、错误最早出现在哪一步**。
工具入参单独高亮：黑盒看不到它，白盒靠它定位理解错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.run_intent_eval import TRACE_PATH  # noqa: E402


# 每个事件的摘要写法：从 payload 里挑最该看的一两个字段，不做全量 dump
PAYLOAD_KEYS = {
    ("planner", "request"): ("text",),
    ("planner", "plan_request"): ("model", "prompt_name", "prompt_version", "prompt_hash"),
    ("planner", "plan_response"): ("raw_text", "duration_ms", "usage"),
    ("planner", "parse_result"): ("ok", "need"),
    ("tool", "tool_call"): ("tool", "arguments"),
    ("tool", "tool_result"): ("count", "names", "reason"),
    ("generator", "generate_request"): ("model", "prompt_name", "prompt_version", "input"),
    ("generator", "generate_response"): ("raw_text", "duration_ms", "usage"),
    ("generator", "parse_result"): ("ok", "total_min", "target_duration_min", "estimated_duration_min", "action_count"),
    ("generator", "result"): ("ok", "action_count"),
    ("planner", "result"): ("ok", "error_code"),
    ("routine", "request"): ("text",),
    ("routine", "routine_request"): ("model", "prompt_version", "prompt_hash", "case_id", "fixture", "arm"),
    ("llm", "llm_round"): ("round", "tool_calls", "text", "duration_ms", "usage"),
    ("tool", "tool_attempt"): ("round", "attempt", "tool", "args", "ok", "error_code", "message"),
    ("routine", "result"): ("ok", "error_code", "rounds", "writes", "text"),
    ("eval", "evaluation"): ("verdict", "failures"),
}

STATE_EVENTS = {"state_before", "state_after"}


def format_units(units: list[dict[str, Any]]) -> str:
    """全量顺序一行看完：paused 单元加括号，一眼看出可见与不可见。"""
    return " → ".join(
        u["unit_id"] if u.get("status") == "active" else f"({u['unit_id']})" for u in units
    )

ERROR_EVENTS = {"result"}


def load_events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"trace file not found: {path}")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    return events


def trace_ids(events: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for event in events:
        if event["trace_id"] not in seen:
            seen.append(event["trace_id"])
    return seen


def pick(events: list[dict[str, Any]], trace_id: str) -> list[dict[str, Any]]:
    return [event for event in events if event["trace_id"] == trace_id]


def summarize(event: dict[str, Any]) -> str:
    keys = PAYLOAD_KEYS.get((event["node"], event["event"]))
    payload = event.get("payload") or {}
    if event["node"] == "state" and event["event"] in STATE_EVENTS:
        return format_units(payload.get("units") or [])
    if keys is None:
        return json.dumps(payload, ensure_ascii=False)
    picked = {key: payload[key] for key in keys if key in payload}
    # usage 单行太长，只留总量；raw_text 是模型原文，原样留但不缩进
    if isinstance(picked.get("usage"), dict):
        picked["usage"] = {"total_tokens": picked["usage"].get("total_tokens")}
    return json.dumps(picked, ensure_ascii=False)


def render(trace_id: str, events: list[dict[str, Any]]) -> str:
    lines = [f"# trace {trace_id}", ""]
    if not events:
        lines.append("（没有事件）")
        return "\n".join(lines)

    first_error = None
    for event in events:
        marker = " "
        payload = event.get("payload") or {}
        if event["node"] == "tool" and event["event"] == "tool_call":
            marker = "*"  # 工具入参：白盒的落点
        elif event["node"] == "tool" and event["event"] == "tool_attempt":
            marker = "*" if payload.get("ok") else "!"  # ! 为被拒的尝试
        if event["event"] in ERROR_EVENTS and payload.get("ok") is False and first_error is None:
            first_error = f"{event['node']}/{event['event']}"
        lines.append(
            f"{marker} [{event['node']:>9}] {event['event']:<18} {summarize(event)}"
        )

    evaluations = [e["payload"] for e in events
                   if e["node"] == "eval" and e["event"] == "evaluation"]
    lines.extend(["", "## 结论", ""])
    lines.append(f"- 事件数：{len(events)}")
    if first_error:
        lines.append(f"- 执行异常步骤：{first_error}")
    else:
        lines.append("- 执行异常步骤：无（不代表业务正确）")
    if evaluations:
        evaluation = evaluations[-1]
        lines.append(f"- 评测结果：{evaluation['verdict']}")
        for failure in evaluation["failures"]:
            lines.append(f"- 评测失败步骤：{failure['step']}，断言：{failure['check']}")
    else:
        lines.append("- 评测结果：未关联")
    attempts = [e["payload"] for e in events if e["node"] == "tool" and e["event"] == "tool_attempt"]
    writes = [a for a in attempts if a["tool"] == "set_routine_order"]
    if attempts:
        states = {e["event"]: e["payload"].get("units") or [] for e in events
                  if e["node"] == "state" and e["event"] in STATE_EVENTS}
        before = states.get("state_before", [])
        lines.append(f"- 操作前全量顺序：{format_units(before)}（括号为 paused）")
        for a in writes:
            order = (a.get("args") or {}).get("order") or []
            missing = [u["unit_id"] for u in before if u["unit_id"] not in order]
            status = "通过" if a["ok"] else f"被拒 {a.get('error_code')}"
            lines.append(
                f"- 第 {a['round']} 轮 写入第 {a['attempt']} 次：{status}；提交 {len(order)} 项"
                + (f"，缺 {', '.join(missing)}" if missing else "")
            )
        if writes and not writes[0]["ok"] and any(w["ok"] for w in writes):
            lines.append("- 首次写入被拒、重试后成功：端到端结果看不出这次失败")
        lines.append(f"- 操作后全量顺序：{format_units(states.get('state_after', []))}")
    tool_calls = [e for e in events if e["node"] == "tool" and e["event"] == "tool_call"]
    if tool_calls:
        args = tool_calls[0]["payload"]["arguments"]
        lines.append(
            f"- 工具入参（理解结果）：muscle={args.get('muscle')} difficulty={args.get('difficulty')}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("trace_id", nargs="?")
    parser.add_argument("--last", action="store_true", help="渲染最近一条 trace")
    parser.add_argument("--path", default=str(TRACE_PATH))
    args = parser.parse_args()

    events = load_events(Path(args.path))
    if args.last:
        ids = trace_ids(events)
        if not ids:
            raise SystemExit("trace file is empty")
        trace_id = ids[-1]
    elif args.trace_id:
        trace_id = args.trace_id
    else:
        parser.error("need a trace_id or --last")

    print(render(trace_id, pick(events, trace_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
