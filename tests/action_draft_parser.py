"""从动作库草稿（docs/动作库-v2-草稿.md）解析动作表，生成或核对 backend/data/action_library_v2.json。

草稿是唯一内容源：改动作先改草稿，再运行 `.venv/bin/python -m tests.action_draft_parser` 重新生成数据文件；
单测用同一函数比对，防止两边漂移。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DRAFT_PATH = ROOT / "docs" / "动作库-v2-草稿.md"
DATA_PATH = ROOT / "backend" / "data" / "action_library_v2.json"
SECTION_TYPE = {"热身": "热身", "有氧与全身": "有氧", "拉伸": "拉伸"}


def _jump_ids(markdown: str) -> set[str]:
    line = next(l for l in markdown.splitlines() if l.startswith("| 标签 |"))
    return set(re.findall(r"[a-z_]+", line.split("带「跳跃」标签：")[1]))


def parse_draft(markdown: str) -> list[dict]:
    jump = _jump_ids(markdown)
    section = markdown.split("## 5. 动作清单")[1]
    body = section.split("### 5.11 替代关系")[0]
    subs = section.split("### 5.11 替代关系")[1].split("### 5.12")[0]
    actions, category = [], None
    for line in body.splitlines():
        match = re.match(r"### 5\.\d+ (\S+)（", line)
        if match:
            category = match.group(1)
            continue
        if not line.startswith("| ") or line.startswith("| id"):
            continue
        sid, name, parts, equipment, difficulty, measure, secs, goals, cue = (
            cell.strip() for cell in line.strip("|").split("|"))
        kind = SECTION_TYPE.get(category, "力量")
        if category == "健身房器械" and measure == "计时":
            kind = "有氧"
        actions.append({
            "id": sid, "name": name.replace("（V7）", ""), "type": kind,
            "parts": parts.split("、"),
            "equipment": [] if equipment == "徒手" else equipment.split("、"),
            "difficulty": difficulty, "measure": "reps" if measure == "次数" else "timed",
            "seconds": int(re.match(r"(\d+)", secs).group(1)),
            "unilateral": "单侧动作" in cue, "goals": goals.split("、"), "cue": cue,
            "tags": ["跳跃"] if sid in jump else [], "substitute": None, "v7": "（V7）" in name,
        })
    by_name = {a["name"]: a for a in actions}
    for line in subs.splitlines():
        if not line.startswith("| ") or line.startswith("| 动作"):
            continue
        names, _, substitute, _ = (cell.strip() for cell in line.strip("|").split("|"))
        sub_id = None if substitute == "无" else by_name[substitute]["id"]
        for name in names.split("、"):
            by_name[name.replace("（V7）", "")]["substitute"] = sub_id
    return actions


def parse_aliases(markdown: str, actions: list[dict]) -> dict[str, str]:
    by_name = {a["name"]: a["id"] for a in actions}
    table = markdown.split("### 5.12 常用叫法")[1].split("## 6.")[0]
    aliases = {}
    for line in table.splitlines():
        if not line.startswith("| ") or line.startswith("| 叫法"):
            continue
        alias, name = (cell.strip() for cell in line.strip("|").split("|"))
        aliases[alias] = by_name[name]
    return aliases


def build() -> dict:
    markdown = DRAFT_PATH.read_text(encoding="utf-8")
    actions = parse_draft(markdown)
    return {"version": "v2", "source": "docs/动作库-v2-草稿.md",
            "actions": actions, "aliases": parse_aliases(markdown, actions)}


if __name__ == "__main__":
    DATA_PATH.write_text(json.dumps(build(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {DATA_PATH.relative_to(ROOT)}")
