"""动作库 v2：数据、候选筛选、时长核算与红线／数据校验（V10）。

内容源是 docs/动作库-v2-草稿.md，数据文件由 tests/action_draft_parser.py 生成。
这里只放确定性代码：红线与数据由代码保证，编排交给模型（见 V10 PRD 第 2 节）。
V7 的 backend/action_lib.py 不受影响。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


DATA_PATH = Path(__file__).parent / "data" / "action_library_v2.json"

HOME_EQUIPMENT = frozenset({"哑铃", "弹力带", "壶铃", "凳子", "跳绳"})
DIFFICULTY_RANK = {"简单": 0, "中等": 1, "中高": 2}
# 红线 R3：只拦「更难」，更简单的动作（热身、拉伸）任何水平都可用
LEVEL_MAX_DIFFICULTY = {"新手": "中等", "进阶": "中高"}
SEGMENTS = ("warmup", "main", "cooldown")
# 段落中文名由代码给出，回复与页面统一使用，不让模型自己起名（主人确认：热身 → 训练 → 拉伸）
SEGMENT_LABELS = {"warmup": "热身", "main": "训练", "cooldown": "拉伸"}
FORMATS = ("straight", "circuit")
TRANSITION_SEC = 15
GYM_SETUP_SEC = 30
# 主人确认（2026-09-25）：AI 生成允许时长有出入，只拦离谱的；30 分钟允许 21～39 分钟
DURATION_TOLERANCE = 0.30

# 数据 D2：组次范围。新手上限取自 ACSM 2009 对新手的建议（每动作 ≤3 组、每组 ≤15 次），作为安全上限
LIMITS = {
    "新手": {"sets": 3, "reps": 15, "rounds": 3},
    "进阶": {"sets": 5, "reps": 20, "rounds": 5},
}
SECONDS_RANGE = (10, 600)
REST_RANGE = (0, 180)


@lru_cache(maxsize=1)
def load_actions() -> dict[str, dict[str, Any]]:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {action["id"]: action for action in data["actions"]}


@lru_cache(maxsize=1)
def load_aliases() -> dict[str, str]:
    return json.loads(DATA_PATH.read_text(encoding="utf-8")).get("aliases", {})


def match_actions(name: str) -> list[dict[str, Any]]:
    """按名称找动作：常用叫法 → 名称完全相同 → 名称包含（任一方向）。可能返回多个，由调用方决定反问或取共同部位。"""
    key = name.strip().replace(" ", "") if isinstance(name, str) else ""
    if not key:
        return []
    actions = load_actions()
    if key in load_aliases():
        return [actions[load_aliases()[key]]]
    exact = [a for a in actions.values() if a["name"] == key]
    if exact:
        return exact
    return [a for a in actions.values() if key in a["name"] or a["name"] in key]


def infer_parts(name: str) -> set[str] | None:
    """记录里的动作名推断部位；多个匹配取共同部位，推不出返回 None（记「未识别」，不猜）。"""
    matched = match_actions(name)
    if not matched:
        return None
    common = set.intersection(*(set(a["parts"]) for a in matched))
    return common or None


def tier(action: dict[str, Any]) -> str:
    equipment = set(action["equipment"])
    if not equipment:
        return "徒手"
    return "家用" if equipment <= HOME_EQUIPMENT else "健身房"


def all_equipment() -> set[str]:
    return {item for action in load_actions().values() for item in action["equipment"]}


def equipment_ok(action: dict[str, Any], available: set[str]) -> bool:
    return set(action["equipment"]) <= available


def difficulty_ok(action: dict[str, Any], level: str) -> bool:
    return DIFFICULTY_RANK[action["difficulty"]] <= DIFFICULTY_RANK[LEVEL_MAX_DIFFICULTY[level]]


def excluded(action: dict[str, Any], exclusions: list[str]) -> bool:
    """排除项可以是标签（如「跳跃」）或部位（如「下肢」）。"""
    return any(item in action["tags"] or item in action["parts"] for item in exclusions)


def candidates(available: set[str], level: str, exclusions: list[str]) -> list[dict[str, Any]]:
    return [a for a in load_actions().values()
            if equipment_ok(a, available) and difficulty_ok(a, level) and not excluded(a, exclusions)]


# ---------- 时长（数据 D1）：只由代码算，模型不自报 ----------


def work_seconds(action: dict[str, Any], item: dict[str, Any]) -> int:
    """一组的做功时间。单侧动作左右各做一遍，乘 2。"""
    side = 2 if action["unilateral"] else 1
    if action["measure"] == "reps":
        return item["reps"] * action["seconds"] * side
    return item.get("seconds", action["seconds"]) * side


def plan_seconds(plan: dict[str, Any]) -> int:
    """直列：Σ(组数 × 每组 + (组数 − 1) × 休息)；循环：轮数 × Σ(每个动作 + 其后休息) + (轮数 − 1) × 轮间休息。
    直列的每个动作与每个循环各算一块，块之间切换 15 秒；每个健身房档动作加 30 秒准备。"""
    actions = load_actions()
    total = 0
    blocks = 0
    gym = set()
    for segment in plan["segments"]:
        if segment.get("format", "straight") == "circuit":
            per_round = sum(work_seconds(actions[i["id"]], i) + i.get("rest_sec", 0) for i in segment["items"])
            total += segment["rounds"] * per_round + (segment["rounds"] - 1) * segment.get("rest_between_rounds", 0)
            blocks += 1
        else:
            for item in segment["items"]:
                work = work_seconds(actions[item["id"]], item)
                total += item["sets"] * work + (item["sets"] - 1) * item.get("rest_sec", 0)
                blocks += 1
        gym.update(i["id"] for i in segment["items"] if tier(actions[i["id"]]) == "健身房")
    return total + TRANSITION_SEC * max(blocks - 1, 0) + GYM_SETUP_SEC * len(gym)


# ---------- 校验：红线 R1–R4、数据 D1、D2、D4 ----------


def _violation(rule: str, detail: str, item_id: str | None = None) -> dict[str, Any]:
    return {"rule": rule, "detail": detail, "id": item_id}


def _check_structure(plan: Any, level: str) -> list[dict[str, Any]]:
    """D4 结构与 D2 取值；结构不合法时后续规则无从判断，先返回。"""
    if not isinstance(plan, dict) or not isinstance(plan.get("segments"), list) or not plan["segments"]:
        return [_violation("D4", "缺少 segments")]
    out = []
    limits = LIMITS[level]
    if not any(s.get("segment") == "main" and s.get("items") for s in plan["segments"] if isinstance(s, dict)):
        out.append(_violation("D4", "没有主训练段"))
    for segment in plan["segments"]:
        if not isinstance(segment, dict) or segment.get("segment") not in SEGMENTS:
            out.append(_violation("D4", f"段落取值不合法：{segment!r}"[:120]))
            continue
        fmt = segment.get("format", "straight")
        if fmt not in FORMATS or not isinstance(segment.get("items"), list) or not segment["items"]:
            out.append(_violation("D4", f"{segment['segment']} 段格式不合法或没有动作"))
            continue
        if fmt == "circuit":
            rounds = segment.get("rounds")
            if not isinstance(rounds, int) or not 1 <= rounds <= limits["rounds"]:
                out.append(_violation("D2", f"循环轮数 {rounds!r} 超出 1～{limits['rounds']}"))
            rest = segment.get("rest_between_rounds", 0)
            if not isinstance(rest, int) or not REST_RANGE[0] <= rest <= REST_RANGE[1]:
                out.append(_violation("D2", f"轮间休息 {rest!r} 超出 {REST_RANGE[0]}～{REST_RANGE[1]} 秒"))
        for item in segment["items"]:
            out.extend(_check_item(item, fmt, limits))
    return out


def _check_item(item: Any, fmt: str, limits: dict[str, int]) -> list[dict[str, Any]]:
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return [_violation("D4", f"动作条目不合法：{item!r}"[:120])]
    action = load_actions().get(item["id"])
    if action is None:
        return [_violation("R1", f"动作 {item['id']} 不在动作库中", item["id"])]
    out = []
    name = action["name"]
    if fmt == "straight":
        sets = item.get("sets")
        if not isinstance(sets, int) or not 1 <= sets <= limits["sets"]:
            out.append(_violation("D2", f"{name} 组数 {sets!r} 超出 1～{limits['sets']}", item["id"]))
    if action["measure"] == "reps":
        reps = item.get("reps")
        if not isinstance(reps, int) or not 1 <= reps <= limits["reps"]:
            out.append(_violation("D2", f"{name} 次数 {reps!r} 超出 1～{limits['reps']}", item["id"]))
    else:
        seconds = item.get("seconds", action["seconds"])
        if not isinstance(seconds, int) or not SECONDS_RANGE[0] <= seconds <= SECONDS_RANGE[1]:
            out.append(_violation("D2", f"{name} 秒数 {seconds!r} 超出 {SECONDS_RANGE[0]}～{SECONDS_RANGE[1]}", item["id"]))
    rest = item.get("rest_sec", 0)
    if not isinstance(rest, int) or not REST_RANGE[0] <= rest <= REST_RANGE[1]:
        out.append(_violation("D2", f"{name} 休息 {rest!r} 超出 {REST_RANGE[0]}～{REST_RANGE[1]} 秒", item["id"]))
    return out


def validate_plan(plan: Any, need: dict[str, Any], available: set[str]) -> dict[str, Any]:
    """返回 {violations, computed_min}。computed_min 只在结构与数据合法时给出。"""
    level = need["level"]
    violations = _check_structure(plan, level)
    if violations:
        return {"violations": violations, "computed_min": None}
    actions = load_actions()
    main_parts: set[str] = set()
    for segment in plan["segments"]:
        for item in segment["items"]:
            action = actions[item["id"]]
            if not equipment_ok(action, available):
                missing = "、".join(sorted(set(action["equipment"]) - available))
                violations.append(_violation("R2", f"{action['name']} 需要 {missing}，用户没有", item["id"]))
            if not difficulty_ok(action, level):
                violations.append(_violation("R3", f"{action['name']} 难度 {action['difficulty']} 超出{level}上限", item["id"]))
            if excluded(action, need["exclusions"]):
                violations.append(_violation("R4", f"{action['name']} 属于用户排除的「{'、'.join(need['exclusions'])}」", item["id"]))
            if segment["segment"] == "main":
                main_parts.update(action["parts"])
    for part in need["parts"]:
        if part != "全身" and part not in main_parts:
            violations.append(_violation("R4", f"用户指定的部位「{part}」没有练到"))
    computed = round(plan_seconds(plan) / 60, 1)
    target = need["duration_min"]
    if abs(computed - target) > target * DURATION_TOLERANCE:
        violations.append(_violation(
            "D1", f"计算时长 {computed} 分钟，目标 {target} 分钟，偏差超过 ±{int(DURATION_TOLERANCE * 100)}%"
                  f"（允许 {round(target * (1 - DURATION_TOLERANCE), 1)}～{round(target * (1 + DURATION_TOLERANCE), 1)} 分钟）"))
    return {"violations": violations, "computed_min": computed}


def enrich(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """数据 D3：动作属性由代码从库里填，模型只给 id 与编排参数。"""
    actions = load_actions()
    segments = []
    for segment in plan["segments"]:
        items = []
        for item in segment["items"]:
            action = actions[item["id"]]
            items.append({**{k: item[k] for k in ("id", "sets", "reps", "seconds", "rest_sec") if k in item},
                          "name": action["name"], "parts": action["parts"], "equipment": action["equipment"],
                          "difficulty": action["difficulty"], "measure": action["measure"],
                          "unilateral": action["unilateral"], "cue": action["cue"]})
        segments.append({k: segment[k] for k in ("segment", "format", "rounds", "rest_between_rounds") if k in segment}
                        | {"label": SEGMENT_LABELS[segment["segment"]], "items": items})
    return segments


# ---------- 时长微调：数据层由代码保证，只调数字不换动作 ----------

REST_STEP = 15
MAX_FIT_STEPS = 60


def _knobs(plan: dict[str, Any], level: str) -> list[tuple[dict[str, Any], str, int, int, int]]:
    """主训练段里可调的数字：(所在对象, 字段, 步长, 下限, 上限)。热身、放松与动作选择不动。"""
    limits = LIMITS[level]
    knobs = []
    for segment in plan["segments"]:
        if segment["segment"] != "main":
            continue
        if segment.get("format", "straight") == "circuit":
            knobs.append((segment, "rounds", 1, 1, limits["rounds"]))
            knobs.append((segment, "rest_between_rounds", REST_STEP, *REST_RANGE))
            for item in segment["items"]:
                knobs.append((item, "rest_sec", REST_STEP, *REST_RANGE))
        else:
            for item in segment["items"]:
                knobs.append((item, "sets", 1, 1, limits["sets"]))
                knobs.append((item, "rest_sec", REST_STEP, *REST_RANGE))
    return knobs


def fit_duration(plan: dict[str, Any], need: dict[str, Any]) -> dict[str, Any] | None:
    """模型选好动作后，若只是时长不在区间内，在 D2 范围内贪心调整轮数、组数与休息，逼近目标。
    返回调整后的计划与改动记录；调不进区间返回 None，交给重生成。"""
    fitted = json.loads(json.dumps(plan))
    target = need["duration_min"] * 60
    low, high = target * (1 - DURATION_TOLERANCE), target * (1 + DURATION_TOLERANCE)
    changes = []
    for _ in range(MAX_FIT_STEPS):
        current = plan_seconds(fitted)
        if low <= current <= high:
            return {"plan": fitted, "changes": changes}
        direction = 1 if current < target else -1
        best = None
        for obj, key, step, lower, upper in _knobs(fitted, need["level"]):
            value = obj.get(key, 0)
            new = value + direction * step
            if not lower <= new <= upper:
                continue
            obj[key] = new
            gap = abs(plan_seconds(fitted) - target)
            obj[key] = value
            if best is None or gap < best[0]:
                best = (gap, obj, key, value, new)
        if best is None or best[0] >= abs(current - target):
            return None
        _, obj, key, old, new = best
        obj[key] = new
        changes.append({"field": key, "target": obj.get("id", "main"), "from": old, "to": new})
    return None
