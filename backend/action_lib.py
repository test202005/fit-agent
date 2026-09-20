from __future__ import annotations

from typing import Any


# 本迭代只演示可观测性方法，不建真实动作库：5 肌群 × 3 难度 = 15 个格子，
# 12 个动作铺满大部分格子，留 3 个空格子用来产生「合法但查不到」的路径。
MUSCLES = ("下肢", "胸", "背", "肩", "核心")
DIFFICULTIES = ("简单", "中等", "中高")
LEVELS = ("新手", "进阶")

# 用户水平到可选难度的映射（PRD 第 9 节已定口径）
LEVEL_DIFFICULTY: dict[str, tuple[str, ...]] = {
    "新手": ("简单", "中等"),
    "进阶": ("中等", "中高"),
}

ACTION_LIB: list[dict[str, str]] = [
    {"name": "徒手深蹲", "muscle_target": "下肢", "difficulty": "简单",
     "risk": "膝盖不要内扣，下蹲到大腿与地面平行即可"},
    {"name": "高脚杯深蹲", "muscle_target": "下肢", "difficulty": "中等",
     "risk": "哑铃贴胸，躯干保持直立，不要含腰"},
    {"name": "保加利亚分腿蹲", "muscle_target": "下肢", "difficulty": "中高",
     "risk": "后脚搭凳不稳时扶墙，前膝不超过脚尖"},
    {"name": "跪姿俯卧撑", "muscle_target": "胸", "difficulty": "简单",
     "risk": "膝关节着地，身体成一条直线，不要塌腰"},
    {"name": "哑铃卧推", "muscle_target": "胸", "difficulty": "中等",
     "risk": "肩胛后收下沉，下放到胸侧，不要弹震"},
    {"name": "双杠臂屈伸", "muscle_target": "胸", "difficulty": "中高",
     "risk": "身体前倾，下放到肩略低于肘，肩部不适立即停止"},
    {"name": "弹力带划船", "muscle_target": "背", "difficulty": "简单",
     "risk": "先动肩胛再屈肘，不要耸肩"},
    {"name": "单臂哑铃划船", "muscle_target": "背", "difficulty": "中高",
     "risk": "脊柱保持中立，不要用腰部甩动"},
    {"name": "哑铃侧平举", "muscle_target": "肩", "difficulty": "简单",
     "risk": "抬到与肩同高即可，不要耸肩借力"},
    {"name": "哑铃推举", "muscle_target": "肩", "difficulty": "中等",
     "risk": "核心收紧，不要过度挺腰"},
    {"name": "平板支撑", "muscle_target": "核心", "difficulty": "简单",
     "risk": "不要塌腰或撅臀，保持骨盆中立"},
    {"name": "死虫式", "muscle_target": "核心", "difficulty": "中等",
     "risk": "腰部始终贴地，动作放慢"},
]

ACTION_FIELDS = ("name", "muscle_target", "difficulty", "risk")


def query_action_lib(muscle: str, difficulty: str) -> dict[str, Any]:
    """按肌群 + 难度查动作库。查不到返回空列表，不当异常。

    非法标签与「合法但没匹配」分开记在 reason 里：两者都返回空，
    但归因结论完全不同——前者是模型用错了标签，后者是动作库确实没有。
    """
    if muscle not in MUSCLES or difficulty not in DIFFICULTIES:
        return {"count": 0, "actions": [], "reason": "unknown_label"}
    matched = [
        dict(action)
        for action in ACTION_LIB
        if action["muscle_target"] == muscle and action["difficulty"] == difficulty
    ]
    if not matched:
        return {"count": 0, "actions": [], "reason": "no_match"}
    return {"count": len(matched), "actions": matched, "reason": "ok"}


def action_names(result: dict[str, Any]) -> list[str]:
    return [action["name"] for action in result.get("actions", [])]


def lookup_action(name: str) -> dict[str, str] | None:
    for action in ACTION_LIB:
        if action["name"] == name:
            return dict(action)
    return None


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_action_lib",
        "description": "按目标肌群和训练难度查询本地动作库，返回匹配的动作列表。",
        "parameters": {
            "type": "object",
            "properties": {
                "muscle": {"type": "string", "enum": list(MUSCLES), "description": "目标肌群"},
                "difficulty": {
                    "type": "string",
                    "enum": list(DIFFICULTIES),
                    "description": "训练难度",
                },
            },
            "required": ["muscle", "difficulty"],
            "additionalProperties": False,
        },
    },
}
