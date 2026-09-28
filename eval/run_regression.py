"""零 token 一键回归：跑单测与六套 stub 评测，汇总成一张表。

用法：
    .venv/bin/python eval/run_regression.py

结果与基线对照见 eval/reports/零token回归基线.md。汇总写入 eval/results/（Git 忽略）。
任一项失败（单测失败、Runner 退出码非 0）则本脚本返回 1。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = ROOT / "eval" / "results"

RUNNERS = [
    ("意图识别", "run_intent_eval.py"),
    ("抽取与受控写入", "run_extract_eval.py"),
    ("查询规划与执行", "run_query_eval.py"),
    ("单轮 Tool Use", "run_tool_eval.py"),
    ("训练计划生成", "run_plan_eval.py"),
    ("训练安排调整", "run_routine_eval.py"),
]
VERDICTS = ("PASS", "REVIEW", "FAIL", "ERROR")


def run_pytest() -> dict:
    proc = subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=ROOT, capture_output=True, text=True)
    tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    passed = re.search(r"(\d+) passed", tail)
    failed = re.search(r"(\d+) failed", tail)
    return {"passed": int(passed.group(1)) if passed else 0,
            "failed": int(failed.group(1)) if failed else 0,
            "exit": proc.returncode, "tail": tail}


def parse_runner_output(stdout: str) -> dict:
    """取 Runner 最后一行 JSON，汇总第一轮的判定结果。"""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            data = json.loads(line)
            metrics = data["metrics"][0]["metrics"]
            row = {"total": metrics.get("total", 0), "report": data.get("report_path", "")}
            row.update({v: metrics.get("verdicts", {}).get(v, 0) for v in VERDICTS})
            if "fault_injection_total" in metrics:
                row["fault"] = f'{metrics["fault_injection_passed"]}/{metrics["fault_injection_total"]}'
            return row
    raise ValueError("Runner 没有输出结果 JSON")


def run_runner(script: str) -> dict:
    proc = subprocess.run([sys.executable, f"eval/{script}", "--views", "all", "--run-mode", "stub"],
                          cwd=ROOT, capture_output=True, text=True)
    try:
        row = parse_runner_output(proc.stdout)
    except (ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
        row = {"total": 0, **{v: 0 for v in VERDICTS}, "report": "", "error": str(exc)}
    row["exit"] = proc.returncode
    return row


def render(pytest_row: dict, rows: list[tuple[str, str, dict]]) -> str:
    lines = [
        f"单测：{pytest_row['passed']} passed，{pytest_row['failed']} failed（退出码 {pytest_row['exit']}）",
        "",
        "| 评测 | Runner | 条数 | PASS | REVIEW | FAIL | ERROR | 故障检测 | 退出码 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, script, r in rows:
        lines.append(f"| {name} | `{script}` | {r['total']} | {r['PASS']} | {r['REVIEW']} | {r['FAIL']} | "
                     f"{r['ERROR']} | {r.get('fault', '-')} | {r['exit']} |")
    ok = pytest_row["exit"] == 0 and all(r["exit"] == 0 for _, _, r in rows)
    lines += ["", "结论：" + ("全部通过" if ok else "有失败项，查看对应 Runner 的报告")]
    return "\n".join(lines)


def main() -> int:
    pytest_row = run_pytest()
    rows = [(name, script, run_runner(script)) for name, script in RUNNERS]
    table = render(pytest_row, rows)

    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip() or "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"regression-summary-{stamp}.md"
    reports = "\n".join(f"- {name}：`{r['report']}`" for name, _, r in rows if r["report"])
    out.write_text(f"# 零 token 回归汇总\n\n- 时间（UTC）：{stamp}\n- Git commit：{commit}\n\n{table}\n\n"
                   f"## 各 Runner 报告\n\n{reports}\n", encoding="utf-8")

    print(table)
    print(f"\n汇总：{out.relative_to(ROOT)}")
    ok = pytest_row["exit"] == 0 and all(r["exit"] == 0 for _, _, r in rows)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
