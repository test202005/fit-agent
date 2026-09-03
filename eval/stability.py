"""多轮与多模型的聚合口径：把「跑了几轮」变成「稳不稳、贵不贵」。

单轮通过率只回答「能不能做到」。Agent 是非确定性系统，同一条 Case 跑三轮可能
三个结果，只报其中一轮等于让读者自己去猜其余两轮。这里定两个口径，都报，不合并：

- pass@k：k 轮里至少一轮通过 —— 峰值能力（Chen et al., Codex 2021 提出 pass@k）
- pass^k：k 轮全部通过 —— 稳定可用（Yao et al., τ-bench 2024 提出 pass^k 衡量一致性）

只报 pass@k 会高估（把偶然成功说成能力），只报 pass^k 会掩盖「其实做到过」。
两者之差就是 flaky 区间，那才是真正要看的东西。

四态在多轮下的处理沿用既有纪律：ERROR 是环境异常、REVIEW 是待人工复核，
两者都不代表业务失败，所以按轮剔除；一条 Case 若没有任何可判轮次，
记为 unevaluated 并退出分母，不拿环境问题稀释通过率。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


EVALUABLE = {"PASS", "FAIL"}


def group_by_case(results: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """按 (模型, case_id) 归组。多模型跑同一套集时，稳定性必须分模型算。"""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in results:
        grouped[(row.get("model", "unknown"), row["case_id"])].append(row)
    return dict(grouped)


def case_stability(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """单条 Case 跨轮的稳定性画像。"""
    evaluable = [r for r in rows if r["verdict"] in EVALUABLE]
    passed = [r for r in evaluable if r["verdict"] == "PASS"]
    return {
        "runs": len(rows),
        "evaluable_runs": len(evaluable),
        "passed_runs": len(passed),
        "verdicts": [r["verdict"] for r in rows],
        "unevaluated": not evaluable,
        "pass_any": bool(passed),
        "pass_all": bool(evaluable) and len(passed) == len(evaluable),
        # 同一条 Case 在不同轮给出不同结果：这才是需要盯的对象
        "flaky": bool(passed) and len(passed) != len(evaluable),
    }


def aggregate_stability(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按模型汇总 pass@k / pass^k / flaky 名单。"""
    grouped = group_by_case(results)
    by_model: dict[str, dict[str, Any]] = {}
    for (model, case_id), rows in grouped.items():
        bucket = by_model.setdefault(
            model,
            {"runs": 0, "cases": 0, "evaluated_cases": 0, "unevaluated_cases": [],
             "pass_any_cases": 0, "pass_all_cases": 0, "flaky_cases": [],
             "high_risk_total": 0, "high_risk_pass_all": 0, "high_risk_unstable": []},
        )
        stat = case_stability(rows)
        bucket["runs"] = max(bucket["runs"], stat["runs"])
        bucket["cases"] += 1
        if stat["unevaluated"]:
            bucket["unevaluated_cases"].append(case_id)
            continue
        bucket["evaluated_cases"] += 1
        bucket["pass_any_cases"] += int(stat["pass_any"])
        bucket["pass_all_cases"] += int(stat["pass_all"])
        if stat["flaky"]:
            bucket["flaky_cases"].append({"case_id": case_id, "verdicts": stat["verdicts"]})
        # 关键安全行为不接受「三次里过两次」：高风险 Case 单独立账
        if rows[0].get("risk") == "high":
            bucket["high_risk_total"] += 1
            if stat["pass_all"]:
                bucket["high_risk_pass_all"] += 1
            else:
                bucket["high_risk_unstable"].append(case_id)

    for bucket in by_model.values():
        n = bucket["evaluated_cases"]
        bucket["pass_at_k"] = round(bucket["pass_any_cases"] / n, 4) if n else 0.0
        bucket["pass_power_k"] = round(bucket["pass_all_cases"] / n, 4) if n else 0.0
        bucket["flaky_count"] = len(bucket["flaky_cases"])
        bucket["high_risk_stable"] = (
            bucket["high_risk_total"] == bucket["high_risk_pass_all"]
        )
        bucket["flaky_cases"].sort(key=lambda item: item["case_id"])
        bucket["unevaluated_cases"].sort()
    return by_model


def aggregate_usage(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按模型汇总 token。没有 usage 的行（stub、未调模型的防御路径）不计入分母。"""
    by_model: dict[str, dict[str, Any]] = {}
    for row in results:
        model = row.get("model", "unknown")
        bucket = by_model.setdefault(
            model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        for usage in row.get("usages") or []:
            if not usage:
                continue
            bucket["calls"] += 1
            bucket["prompt_tokens"] += usage.get("prompt_tokens", 0)
            bucket["completion_tokens"] += usage.get("completion_tokens", 0)
            bucket["total_tokens"] += usage.get("total_tokens", 0)
    for model, bucket in by_model.items():
        cases = sum(1 for row in results if row.get("model", "unknown") == model)
        bucket["tokens_per_case"] = round(bucket["total_tokens"] / cases, 1) if cases else 0.0
        bucket["tokens_per_call"] = (
            round(bucket["total_tokens"] / bucket["calls"], 1) if bucket["calls"] else 0.0
        )
    return by_model


def collect_usages(events: list[dict[str, Any]]) -> list[dict[str, int]]:
    """从 trace 事件里捞出本条 Case 的全部 token 消耗。

    成本走 trace 而不是业务返回值：返回结构受契约断言约束（字段集合必须精确匹配），
    往里塞可观测数据会让契约断言失去意义。
    """
    return [
        event["payload"]["usage"]
        for event in events
        if isinstance(event.get("payload"), dict) and event["payload"].get("usage")
    ]


def render_stability(
    stability: dict[str, dict[str, Any]], usage: dict[str, dict[str, Any]], runs: int
) -> list[str]:
    """报告里的「稳定性与成本」一节。四个 Runner 共用。"""
    lines = [
        "## 稳定性与成本", "",
        f"> 跑了 {runs} 轮。pass@k = 至少一轮通过（峰值能力）；"
        "pass^k = 全部轮通过（稳定可用）。两者之差即 flaky 区间。", "",
        "| 模型 | 可判 Case | pass@k | pass^k | flaky | 高风险稳定 | 总 token | token/Case |",
        "|---|---:|---:|---:|---:|:---:|---:|---:|",
    ]
    for model in sorted(stability):
        s = stability[model]
        u = usage.get(model, {})
        lines.append(
            f"| {model} | {s['evaluated_cases']} | {s['pass_at_k']:.4f} | {s['pass_power_k']:.4f} | "
            f"{s['flaky_count']} | {'✅' if s['high_risk_stable'] else '❌'} | "
            f"{u.get('total_tokens', 0)} | {u.get('tokens_per_case', 0):.1f} |"
        )

    lines.extend(["", "### 波动明细", ""])
    any_flaky = False
    for model in sorted(stability):
        s = stability[model]
        for item in s["flaky_cases"]:
            any_flaky = True
            lines.append(f"- `{model}` / `{item['case_id']}`：{' → '.join(item['verdicts'])}")
        for case_id in s["unevaluated_cases"]:
            any_flaky = True
            lines.append(f"- `{model}` / `{case_id}`：全轮 ERROR/REVIEW，未进入通过率分母")
        if not s["high_risk_stable"]:
            any_flaky = True
            lines.append(
                f"- `{model}` 高风险 Case 未做到轮轮通过："
                f"{', '.join(s['high_risk_unstable'])}"
            )
    if not any_flaky:
        lines.append("无波动：所有 Case 在全部轮次结果一致。")
    return lines
