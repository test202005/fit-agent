"""双架构对比实验：同一批输入，固定链路 vs 工具调用。

回答的不是「Agent 怎么写」，而是「什么时候该用 Agent」。
两种架构的执行层完全相同（同样的 storage / query / 校验规则），
唯一变量是决策方式：代码 if/else 还是模型选工具。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.agent import run_agent  # noqa: E402
from backend.clock import FrozenClock  # noqa: E402
from backend.llm import LiveLLM  # noqa: E402
from backend.pipeline import handle_message  # noqa: E402
from backend.storage import FakeStorage  # noqa: E402
from backend.trace import Tracer  # noqa: E402
from eval.run_intent_eval import RESULTS_DIR, TRACE_PATH, git_commit, load_dotenv, safe_div  # noqa: E402

NOW = "2026-09-03T20:00:00+08:00"

# 每条标注：这条输入期望产生几次写入、几次查询，以及固定链路结构上能不能做到
CASES = [
    {"id": "c01", "text": "今天卧推60kg做了4组每组8次", "writes": 1, "queries": 0, "kind": "single"},
    {"id": "c02", "text": "昨晚跑了五公里", "writes": 1, "queries": 0, "kind": "single"},
    {"id": "c03", "text": "今天练了胸", "writes": 1, "queries": 0, "kind": "single"},
    {"id": "c04", "text": "硬拉100kg三组，然后划船做了四组", "writes": 2, "queries": 0, "kind": "single"},
    {"id": "c05", "text": "今天练了什么", "writes": 0, "queries": 1, "kind": "single"},
    {"id": "c06", "text": "昨天的训练记录", "writes": 0, "queries": 1, "kind": "single"},
    {"id": "c07", "text": "这周卧推了几次", "writes": 0, "queries": 1, "kind": "single"},
    {"id": "c08", "text": "卧推标准动作是什么", "writes": 0, "queries": 0, "kind": "single"},
    {"id": "c09", "text": "明天打算练腿", "writes": 0, "queries": 0, "kind": "single"},
    {"id": "c10", "text": "练了3年的卧推，今天做了5组", "writes": 1, "queries": 0, "kind": "single"},
    # 复合请求：一句话两件事，固定链路结构上做不到
    {"id": "c11", "text": "今天练了什么？顺便记一下我刚才跑了3公里", "writes": 1, "queries": 1, "kind": "compound"},
    {"id": "c12", "text": "记一下今天卧推了4组，再看看这周卧推几次", "writes": 1, "queries": 1, "kind": "compound"},
]


def measure(fn) -> tuple[Any, float]:
    started = time.perf_counter()
    out = fn()
    return out, round((time.perf_counter() - started) * 1000, 1)


def run_fixed(case: dict, llm: LiveLLM) -> dict[str, Any]:
    storage = FakeStorage()
    clock = FrozenClock(NOW)
    result, ms = measure(
        lambda: handle_message(
            case["text"], llm, llm, storage, Tracer(TRACE_PATH), query_llm=llm, clock=clock
        )
    )
    queries = 1 if result.get("stage") == "executor" else 0
    return {
        "writes": len(storage.read_all()),
        "queries": queries,
        "ms": ms,
        "ok": bool(result.get("ok")),
    }


def run_tools(case: dict, llm: LiveLLM) -> dict[str, Any]:
    storage = FakeStorage()
    clock = FrozenClock(NOW)
    result, ms = measure(
        lambda: run_agent(case["text"], llm, storage, Tracer(TRACE_PATH), clock, f"t-{case['id']}")
    )
    traj = result.get("trajectory", [])
    return {
        "writes": len(storage.read_all()),
        "queries": sum(1 for s in traj if s["tool"] in {"query_records", "count_exercise"}),
        "ms": ms,
        "ok": bool(result.get("ok")),
    }


def score(case: dict, out: dict) -> bool:
    return out["ok"] and out["writes"] == case["writes"] and out["queries"] == case["queries"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    load_dotenv()
    llm = LiveLLM()

    rows = []
    for case in CASES:
        for run_index in range(1, args.runs + 1):
            fixed, tools = run_fixed(case, llm), run_tools(case, llm)
            rows.append({
                "run": run_index, "id": case["id"], "text": case["text"], "kind": case["kind"],
                "expected": {"writes": case["writes"], "queries": case["queries"]},
                "fixed": fixed, "fixed_pass": score(case, fixed),
                "tools": tools, "tools_pass": score(case, tools),
            })

    def summarize(kind: str | None = None) -> dict[str, Any]:
        subset = [r for r in rows if kind is None or r["kind"] == kind]
        if not subset:
            return {}
        return {
            "n": len(subset),
            "fixed_pass_rate": round(safe_div(sum(r["fixed_pass"] for r in subset), len(subset)), 4),
            "tools_pass_rate": round(safe_div(sum(r["tools_pass"] for r in subset), len(subset)), 4),
            "fixed_avg_ms": round(sum(r["fixed"]["ms"] for r in subset) / len(subset), 1),
            "tools_avg_ms": round(sum(r["tools"]["ms"] for r in subset) / len(subset), 1),
        }

    summary = {"all": summarize(), "single": summarize("single"), "compound": summarize("compound")}

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"arch-compare-{ts}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    lines = [
        "# 架构对比：固定链路 vs 工具调用", "",
        f"- generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"- model: {llm.model}", f"- git_commit: {git_commit()}", f"- runs: {args.runs}",
        f"- 冻结时间: {NOW}", "",
        "> 两种架构的执行层完全相同，唯一变量是决策方式：代码 if/else 还是模型选工具。", "",
        "## 汇总", "",
        "| 场景 | 条数 | 固定链路通过率 | 工具调用通过率 | 固定链路耗时 | 工具调用耗时 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label, key in [("全部", "all"), ("单一意图", "single"), ("复合请求", "compound")]:
        s = summary[key]
        if s:
            lines.append(
                f"| {label} | {s['n']} | {s['fixed_pass_rate']:.4f} | {s['tools_pass_rate']:.4f} | "
                f"{s['fixed_avg_ms']:.0f}ms | {s['tools_avg_ms']:.0f}ms |"
            )
    lines.extend(["", "## 逐条结果", "",
        "| run | id | 输入 | 类型 | 期望 写/查 | 固定链路 | 工具调用 |",
        "|---:|---|---|---|---|---|---|"])
    for r in rows:
        f, t, e = r["fixed"], r["tools"], r["expected"]
        lines.append(
            f"| {r['run']} | {r['id']} | {r['text']} | {r['kind']} | {e['writes']}/{e['queries']} | "
            f"{f['writes']}/{f['queries']} {'✅' if r['fixed_pass'] else '❌'} {f['ms']:.0f}ms | "
            f"{t['writes']}/{t['queries']} {'✅' if r['tools_pass'] else '❌'} {t['ms']:.0f}ms |"
        )
    report = RESULTS_DIR / f"arch-compare-{ts}.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"report": str(report.relative_to(ROOT)), "summary": summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
