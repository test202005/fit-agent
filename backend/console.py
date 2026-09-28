"""本地调试台：用户视角（聊天 + 面板）与评测视角（链路）放在同一个页面对照。

只供本机调试：不改 /api/chat 契约，对话不持久化，留痕以 Trace 为准。
页面只有「智能助手」一个入口；接口仍接受 chain 参数，保留专用链路给对照与复现使用。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from backend.agent import run_agent
from backend.assistant import run_assistant
from backend.deps import get_llm, get_routine_store, plan_compose_llm_for, plan_llm_for
from backend.pipeline import handle_message
from backend.plan import run_workout_plan
from backend.routine_agent import run_routine_agent
from backend.trace import Tracer


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = Path(__file__).parent / "static"
FIXTURES_PATH = ROOT / "eval" / "datasets" / "routine-fixtures.json"
CAPABILITIES_PATH = ROOT / "docs" / "当前能力清单.md"
DEFAULT_FIXTURE = "base7"
CHAINS = {
    "assistant": "智能助手（统一入口，/api/assistant 同款）",
    "auto": "自动路由（/api/chat 同款：记录 / 查询 / 拒识）",
    "routine": "训练安排",
    "tool": "工具调用",
    "plan": "训练计划",
}
TRACE_LIST_LIMIT = 200

bp = Blueprint("console", __name__)


# ---------- 用户看到的回复：把各链路的结构化结果翻成一句话 ----------


def _format_record(record: dict[str, Any]) -> str:
    parts = [record.get("exercise") or "?"]
    units = [("weight_kg", "kg"), ("sets", "组"), ("reps", "次"), ("duration_min", "分钟"), ("distance_km", "公里")]
    parts += [f"{record[field]:g}{unit}" for field, unit in units if record.get(field) is not None]
    return " ".join(parts)


def reply_text(chain: str, result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"处理失败（{result.get('error_code', 'unknown')}）"
    if chain in {"routine", "assistant"}:
        return result.get("text") or "（没有回复）"
    if chain == "tool":
        if result.get("text"):
            return result["text"]
        steps = result.get("trajectory", [])
        return "已执行：" + "、".join(f"{s['tool']}{'✓' if s['ok'] else '✗'}" for s in steps) if steps else "（没有回复）"
    if chain == "plan":
        workout = result.get("workout") or {}
        lines = [f"{a['name']} {a['sets']}组×{a['reps']}次，休息{a['rest_sec']}秒" for a in workout.get("plan", [])]
        if workout.get("note"):
            lines.append(workout["note"])
        return "\n".join(lines) or "没有生成计划"
    intent = result.get("intent")
    if intent == "record":
        records = "；".join(_format_record(r) for r in result.get("records", []))
        state = result.get("state")
        if state == "complete":
            return f"已记录：{records}"
        if state == "incomplete":
            return f"信息不完整，暂未记录：{records}"
        return "没有识别出训练内容，未记录"
    if intent == "query":
        if not result.get("supported", True):
            return "暂不支持这类查询"
        records = result.get("records", [])
        if not records:
            return f"没有查到记录（共 {result.get('count', 0)} 条）"
        return f"查到 {result.get('count', len(records))} 条：" + "；".join(_format_record(r) for r in records)
    return "这个我暂时帮不了，我只负责记录和查询训练。"


def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = 0
    llm_ms = 0.0
    for event in events:
        payload = event.get("payload") or {}
        usage = payload.get("usage")
        if isinstance(usage, dict):
            tokens += usage.get("total_tokens") or 0
        if isinstance(payload.get("duration_ms"), (int, float)) and event.get("node") != "tool":
            llm_ms += payload["duration_ms"]
    return {"tokens": tokens, "llm_ms": round(llm_ms, 1), "events": len(events)}


# ---------- 辅助 ----------


def _load_fixture(name: str = DEFAULT_FIXTURE) -> list[dict[str, Any]]:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))[name]


def _user_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


# ---------- 页面与接口 ----------


@bp.get("/console")
def page():
    return send_from_directory(STATIC_DIR, "console.html")


@bp.get("/console/static/<path:name>")
def static_file(name: str):
    return send_from_directory(STATIC_DIR, name)


@bp.get("/console/api/chains")
def chains():
    return jsonify({"chains": [{"id": k, "label": v} for k, v in CHAINS.items()]})


def parse_capabilities(markdown: str) -> list[dict[str, Any]]:
    """从能力清单「能做什么」表解析，页面与文档同源，不在前端另写一份。"""
    labels = {v.split("（")[0]: k for k, v in CHAINS.items()}
    section = markdown.split("## 能做什么", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("|")][2:]
    items = []
    for line in rows:
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4:
            continue
        chain_label = cells[2].split("/")[0].strip()
        items.append({
            "name": cells[0],
            "examples": [e.strip() for e in cells[1].split("；") if e.strip()],
            "chain": labels.get(chain_label, "auto"),
            "result": cells[3],
        })
    return items


@bp.get("/console/api/capabilities")
def capabilities():
    return jsonify({"capabilities": parse_capabilities(CAPABILITIES_PATH.read_text(encoding="utf-8")),
                    "source": "docs/当前能力清单.md"})


@bp.post("/console/api/run")
def run():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("text"), str) or not payload["text"].strip():
        return jsonify({"ok": False, "error_code": "bad_request"}), 400
    chain = payload.get("chain", "assistant")
    user_id = _user_id(payload.get("user_id", "demo-user"))
    if chain not in CHAINS or user_id is None:
        return jsonify({"ok": False, "error_code": "bad_request"}), 400

    text = payload["text"].strip()
    tracer = Tracer(current_app.config["TRACE_PATH"])
    clock = current_app.config["CLOCK"]
    try:
        if chain == "assistant":
            engine = current_app.config["PLAN_ENGINE"]
            result = run_assistant(text, get_llm(), plan_llm_for(engine), current_app.config["STORAGE"],
                                   get_routine_store(), tracer, clock, user_id=user_id,
                                   context={"arm": "console"}, plan_engine=engine,
                                   plan_compose_llm=plan_compose_llm_for(engine))
        elif chain == "routine":
            result = run_routine_agent(
                text, get_llm(), get_routine_store(), tracer, clock,
                f"t-{uuid.uuid4()}", user_id, context={"arm": "console"},
            )
        elif chain == "tool":
            result = run_agent(text, get_llm(), current_app.config["STORAGE"], tracer, clock,
                               f"t-{uuid.uuid4()}", user_id)
        elif chain == "plan":
            result = run_workout_plan(text, get_llm("PLAN_LLM"), tracer, f"t-{uuid.uuid4()}")
        else:
            client = get_llm()
            result = handle_message(text, client, client, current_app.config["STORAGE"], tracer,
                                    query_llm=client, clock=clock, user_id=user_id)
    except ValueError as exc:
        # 最常见的是没配 DEEPSEEK_API_KEY：说清楚怎么修，不吐堆栈
        return jsonify({"ok": False, "error_code": "config_error", "message": str(exc)}), 500

    return jsonify({
        "ok": bool(result.get("ok")),
        "chain": chain,
        "reply": reply_text(chain, result),
        "trace_id": result.get("trace_id"),
        "error_code": result.get("error_code"),
        "summary": _summary(tracer.events),
    })


@bp.get("/console/api/panel")
def panel():
    """用户可见视图：只列 active 训练单元与已落库的完整训练记录。"""
    user_id = _user_id(request.args.get("user_id", "demo-user"))
    if user_id is None:
        return jsonify({"ok": False, "error_code": "bad_request"}), 400
    units = get_routine_store().read(user_id)
    rows = [r for r in current_app.config["STORAGE"].read_all(user_id) if r.get("state") == "complete"]
    rows.sort(key=lambda r: r.get("ts") or "", reverse=True)
    return jsonify({
        "ok": True,
        "routine_initialized": bool(units),
        "routine": [{"unit_id": u["unit_id"], "name": u["name"]} for u in units if u["status"] == "active"],
        "records": [{"ts": r.get("ts"), "text": _format_record(r)} for r in rows[:8]],
    })


@bp.post("/console/api/reset")
def reset():
    payload = request.get_json(silent=True) or {}
    user_id = _user_id(payload.get("user_id", "demo-user"))
    if user_id is None:
        return jsonify({"ok": False, "error_code": "bad_request"}), 400
    get_routine_store().seed(user_id, _load_fixture())
    return jsonify({"ok": True, "fixture": DEFAULT_FIXTURE})


def _read_traces() -> dict[str, list[dict[str, Any]]]:
    path = Path(current_app.config["TRACE_PATH"])
    grouped: dict[str, list[dict[str, Any]]] = {}
    if not path.is_file():
        return grouped
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                event = json.loads(line)
                grouped.setdefault(event["trace_id"], []).append(event)
    return grouped


@bp.get("/console/api/traces")
def traces():
    grouped = _read_traces()
    ids = list(grouped)[-TRACE_LIST_LIMIT:]
    return jsonify({"traces": [{"id": tid, "events": grouped[tid]} for tid in reversed(ids)]})


@bp.get("/console/api/traces/<trace_id>")
def trace_detail(trace_id: str):
    events = _read_traces().get(trace_id)
    if events is None:
        return jsonify({"ok": False, "error_code": "not_found"}), 404
    return jsonify({"id": trace_id, "events": events})
