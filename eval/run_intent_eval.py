from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.llm import (  # noqa: E402
    MAX_RETRIES,
    MAX_TOKENS,
    TEMPERATURE,
    THINKING_MODE,
    TIMEOUT_SECONDS,
    LiveLLM,
    StubLLM,
)
from backend.router import PROMPT_PATH, route  # noqa: E402
from backend.trace import Tracer  # noqa: E402

from eval.stability import (  # noqa: E402
    aggregate_stability,
    aggregate_usage,
    collect_usages,
    render_stability,
)


DATASET_PATH = ROOT / "eval" / "datasets" / "intent-dataset.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
TRACE_PATH = ROOT / "backend" / "logs" / "trace.jsonl"
LABELS = ["record", "query", "reject"]
ERROR_CODES = {"bad_request", "llm_timeout", "llm_api_error", "llm_parse_error"}
SUCCESS_FIELDS = {"ok", "trace_id", "intent", "confidence", "source"}
FAILURE_FIELDS = {"ok", "trace_id", "error_code"}
EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "logs", "results"}
HIGH_RISK_REJECT_CATEGORIES = {
    "reject-pure-negative",
    "reject-future",
    "reject-consultation",
    "reject-injection",
}


def load_dotenv() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "uncommitted"


def load_cases(view: str, run_mode: str) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in DATASET_PATH.read_text(encoding="utf-8").splitlines()]
    for case in cases:
        if (
            case.get("category") in HIGH_RISK_REJECT_CATEGORIES
            and case.get("risk") != "high"
        ):
            raise ValueError(f"{case['case_id']} must be marked as high risk")
        # 数据质量门禁：每条 Case 必须自带判定方式与来源
        for field in ("assertion", "source"):
            if not case.get(field):
                raise ValueError(f"{case['case_id']} is missing required field {field!r}")
        if case["assertion"] not in ASSERTIONS:
            raise ValueError(
                f"{case['case_id']} declares unknown assertion {case['assertion']!r}"
            )
    selected = []
    for case in cases:
        is_fault = "inject_fault" in case
        if is_fault and run_mode != "stub":
            continue
        if view != "all" and view not in case["views"]:
            continue
        selected.append(case)
    return selected


def snapshot(paths: list[Path]) -> dict[str, int]:
    state: dict[str, int] = {}
    for base in paths:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED_PARTS for part in relative.parts) or path.suffix == ".pyc":
                continue
            state[str(relative)] = path.stat().st_mtime_ns
    return state


def expected_trace_events(result: dict[str, Any]) -> set[str]:
    if result.get("error_code") == "bad_request":
        return {"input_received", "result"}
    if result.get("error_code") in {"llm_timeout", "llm_api_error"}:
        return {"input_received", "llm_request", "result"}
    return {"input_received", "llm_request", "llm_response", "parse_result", "result"}


ENVIRONMENT_ERROR_CODES = {"llm_timeout", "llm_api_error"}

# 判定方式由数据声明，不由代码隐式推断——换执行器时断言跟着数据走
ASSERTIONS = {
    "intent_equals": lambda case, actual: actual.get("intent") == case["expected_intent"],
    "error_code_equals": lambda case, actual: (
        actual.get("error_code") == case["expected_error_code"]
    ),
}


def run_assertion(case: dict[str, Any], actual: dict[str, Any]) -> bool:
    name = case["assertion"]
    if name not in ASSERTIONS:
        raise ValueError(f"{case['case_id']}: unknown assertion {name!r}")
    return ASSERTIONS[name](case, actual)


def assert_trace_consistency(
    result: dict[str, Any], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """第四问：说的和做的是否一致。执行痕迹里记的结论必须与返回值吻合。"""
    checks: list[tuple[str, bool]] = []
    final = next(
        (event for event in reversed(events) if event["event"] == "result"), None
    )
    checks.append(("result_event_present", final is not None))
    if final is None:
        return [{"name": name, "pass": passed} for name, passed in checks]

    payload = final["payload"]
    checks.append(("trace_ok_matches", payload.get("ok") is result.get("ok")))
    if result.get("ok") is True:
        checks.append(("trace_intent_matches", payload.get("intent") == result.get("intent")))
        parsed = next(
            (event for event in events if event["event"] == "parse_result"), None
        )
        checks.append(
            (
                "parse_intent_matches",
                parsed is not None and parsed["payload"].get("intent") == result.get("intent"),
            )
        )
    else:
        checks.append(
            ("trace_error_code_matches", payload.get("error_code") == result.get("error_code"))
        )
    return [{"name": name, "pass": passed} for name, passed in checks]


def decide_verdict(
    case: dict[str, Any],
    result: dict[str, Any],
    assertion_pass: bool,
    contract_pass: bool,
    trace_pass: bool,
    trace_write_failed: bool,
) -> str:
    """四态：把环境异常与评测程序故障，从业务失败里摘出来。"""
    if trace_write_failed:
        return "ERROR"
    # 规则尚未定义唯一 expected 的探索题：跑但不判分，不污染通过率
    if case.get("tier") == "observation":
        return "REVIEW"
    expected_error = case.get("expected_error_code")
    actual_error = result.get("error_code")
    # 本轮没注入故障，却撞上超时/接口错误 —— 环境问题，不是业务失败
    if actual_error in ENVIRONMENT_ERROR_CODES and expected_error != actual_error:
        return "ERROR"
    if assertion_pass and contract_pass and trace_pass:
        return "PASS"
    return "FAIL"


def assert_contract(result: dict[str, Any]) -> list[dict[str, Any]]:
    """第一问：系统承诺了什么。响应结构、枚举与互斥，不看业务语义。"""
    checks: list[tuple[str, bool]] = []
    if result.get("ok") is True:
        checks.append(("structure_success", set(result) == SUCCESS_FIELDS))
        checks.append(("intent_enum", result.get("intent") in LABELS))
        confidence = result.get("confidence")
        checks.append(
            (
                "confidence_range",
                isinstance(confidence, (int, float))
                and not isinstance(confidence, bool)
                and 0 <= confidence <= 1,
            )
        )
        checks.append(("source_value", result.get("source") == "llm"))
    else:
        checks.append(("structure_failure", set(result) == FAILURE_FIELDS))
        checks.append(("error_code_enum", result.get("error_code") in ERROR_CODES))
        # 第三问：失败时不许泄漏业务字段
        checks.append(("no_business_leak", "intent" not in result))
    return [{"name": name, "pass": passed} for name, passed in checks]


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    if run_mode == "stub":
        fault = case.get("inject_fault")
        raw_text = ""
        # 防御类 Case 不该走到模型；剧本留空，一旦真被调用就会落 parse_error 暴露出来
        if not fault and "expected_intent" in case:
            raw_text = json.dumps(
                {"intent": case["expected_intent"], "confidence": 0.9},
                ensure_ascii=False,
            )
        llm = StubLLM(raw_text=raw_text, fault=fault)
    else:
        if live_llm is None:
            raise RuntimeError("live LLM is not initialized")
        llm = live_llm

    tracer = Tracer(TRACE_PATH)
    actual = route(case["input"], llm, tracer)
    assertion_pass = run_assertion(case, actual)
    contract_checks = assert_contract(actual)
    contract_pass = all(check["pass"] for check in contract_checks)
    consistency_checks = assert_trace_consistency(actual, tracer.events)
    consistency_pass = all(check["pass"] for check in consistency_checks)
    event_names = {event["event"] for event in tracer.events}
    # 精确集合比对：多出事件同样算失败，否则「防御路径没调 LLM」断不住
    trace_events_pass = expected_trace_events(actual) == event_names
    trace_pass = trace_events_pass and consistency_pass and not tracer.write_failed
    verdict = decide_verdict(
        case, actual, assertion_pass, contract_pass, trace_pass, tracer.write_failed
    )
    parse_event = next(
        (event for event in tracer.events if event["event"] == "parse_result"), None
    )
    confidence_normalized = bool(
        parse_event and parse_event["payload"].get("confidence_normalized", False)
    )
    return {
        "case_id": case["case_id"],
        "input": case["input"],
        "category": case["category"],
        "risk": case.get("risk", "normal"),
        "views": case["views"],
        "expected": case.get("expected_intent") or case.get("expected_error_code"),
        "actual": actual.get("intent") or actual.get("error_code"),
        "ok": actual.get("ok"),
        "verdict": verdict,
        "pass": verdict == "PASS",
        "semantic_pass": assertion_pass,
        "contract_pass": contract_pass,
        "consistency_pass": consistency_pass,
        "failed_checks": [
            check["name"]
            for check in contract_checks + consistency_checks
            if not check["pass"]
        ],
        "assertion_total": len(contract_checks) + len(consistency_checks) + 2,
        "assertion_passed": (
            sum(1 for c in contract_checks + consistency_checks if c["pass"])
            + int(assertion_pass)
            + int(trace_events_pass)
        ),
        "trace_pass": trace_pass,
        "trace_id": actual["trace_id"],
        "error_code": actual.get("error_code"),
        "confidence_normalized": confidence_normalized,
        "usages": collect_usages(tracer.events),
    }


def safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def add_matrix_args(parser: argparse.ArgumentParser) -> None:
    """四个 Runner 共用的运行矩阵参数：跑几轮、跑哪些模型、用什么温度。"""
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--models",
        default="",
        help="逗号分隔的模型名，仅 live 模式生效；留空则用 DEEPSEEK_MODEL",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=f"覆盖采样温度，仅 live 生效；留空用默认 {TEMPERATURE}。"
        "升温只用于稳定性专项——验证波动检测本身抓不抓得住波动，不用于质量验收",
    )


def build_clients(run_mode: str, models: str, temperature: float | None = None) -> list[Any]:
    """live 模式按 --models 造多个客户端；stub 模式只有一个占位。"""
    if run_mode != "live":
        return [None]
    names = [name.strip() for name in models.split(",") if name.strip()] or [None]
    return [LiveLLM(model=name, temperature=temperature) for name in names]


def effective_temperature(args: argparse.Namespace) -> float:
    """报告必须记录实际生效的温度，不能记常量——否则升温跑出来的报告是假的。"""
    if args.run_mode != "live" or args.temperature is None:
        return TEMPERATURE
    return args.temperature


def client_model(llm: Any) -> str:
    return llm.model if llm is not None else "stub"


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = Counter(result["verdict"] for result in results)
    # 环境异常不进分类指标：超时不代表模型分错了
    evaluated = [result for result in results if result["verdict"] in {"PASS", "FAIL"}]
    classified = [result for result in evaluated if result["expected"] in LABELS]
    confusion = {label: {actual: 0 for actual in LABELS} for label in LABELS}
    parse_errors = 0
    for result in classified:
        if result["error_code"] == "llm_parse_error":
            parse_errors += 1
        elif result["actual"] in LABELS:
            confusion[result["expected"]][result["actual"]] += 1

    per_class: dict[str, dict[str, float]] = {}
    f1_values = []
    for label in LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[expected][label] for expected in LABELS if expected != label)
        fn = sum(confusion[label][actual] for actual in LABELS if actual != label)
        fn += sum(
            1
            for result in classified
            if result["expected"] == label and result["actual"] not in LABELS
        )
        precision = safe_div(tp, tp + fp)
        recall = safe_div(tp, tp + fn)
        f1 = safe_div(2 * precision * recall, precision + recall)
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
        }
        f1_values.append(f1)

    high_risk_into_record = sum(
        1
        for result in classified
        if result["risk"] == "high"
        and result["expected"] != "record"
        and result["actual"] == "record"
    )
    assertion_total = sum(result["assertion_total"] for result in evaluated)
    assertion_passed = sum(result["assertion_passed"] for result in evaluated)
    return {
        "total": len(results),
        "classified_total": len(classified),
        "passed": sum(1 for result in results if result["pass"]),
        "verdicts": {
            state: verdicts.get(state, 0)
            for state in ("PASS", "FAIL", "REVIEW", "ERROR")
        },
        # 两个口径都报：Case 级给产品看影响面，断言级给开发估工作量
        "case_pass_rate": round(
            safe_div(verdicts.get("PASS", 0), len(evaluated)), 4
        ),
        "assertion_pass_rate": round(safe_div(assertion_passed, assertion_total), 4),
        "assertion_total": assertion_total,
        "assertion_passed": assertion_passed,
        "end_to_end_pass_rate": round(
            safe_div(sum(1 for result in results if result["pass"]), len(results)), 4
        ),
        "parse_errors": parse_errors,
        "parse_error_rate": round(safe_div(parse_errors, len(classified)), 4),
        "confidence_normalized_count": sum(
            1 for result in classified if result["confidence_normalized"]
        ),
        "high_risk_into_record": high_risk_into_record,
        "macro_f1": round(sum(f1_values) / len(f1_values), 4),
        "present_labels_macro_f1": round(
            sum(
                per_class[label]["f1"]
                for label in LABELS
                if any(result["expected"] == label for result in classified)
            )
            / sum(
                1
                for label in LABELS
                if any(result["expected"] == label for result in classified)
            ),
            4,
        ),
        "per_class": per_class,
        "confusion_matrix": confusion,
    }


def render_report(
    metadata: dict[str, Any],
    metrics_by_run: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> str:
    lines = [
        "# Intent Eval Report",
        "",
        f"- generated_at: {metadata['generated_at']}",
        f"- view: {metadata['view']}",
        f"- run_mode: {metadata['run_mode']}",
        f"- model: {metadata['model']}",
        f"- git_commit: {metadata['git_commit']}",
        f"- prompt_hash: {metadata['prompt_hash']}",
        f"- dataset_hash: {metadata['dataset_hash']}",
        f"- temperature: {metadata['temperature']}",
        f"- max_tokens: {metadata['max_tokens']}",
        f"- thinking: {metadata['thinking']}",
        f"- timeout_seconds: {metadata['timeout_seconds']}",
        f"- max_retries: {metadata['max_retries']}",
        f"- runs: {metadata['runs']}",
        "",
        "> Stub results validate contracts only; they do not represent model quality."
        if metadata["run_mode"] == "stub"
        else "> Live results represent model behavior for this exact model, prompt and dataset snapshot.",
        "",
    ]
    for entry in metrics_by_run:
        metrics = entry["metrics"]
        lines.extend(
            [
                f"## {entry['model']} · Run {entry['run_index']}",
                "",
                "### 结果口径",
                "",
                f"- 【核心结果】Case 级 {metrics['case_pass_rate']:.4f}"
                f"（PASS {metrics['verdicts']['PASS']} / FAIL {metrics['verdicts']['FAIL']}）",
                f"- 【断言明细】断言级 {metrics['assertion_pass_rate']:.4f}"
                f"（{metrics['assertion_passed']} / {metrics['assertion_total']}）",
                f"- 【待复核】REVIEW {metrics['verdicts']['REVIEW']}",
                f"- 【执行异常】ERROR {metrics['verdicts']['ERROR']}（不计入通过率）",
                "",
                f"- end_to_end_pass_rate: {metrics['end_to_end_pass_rate']:.4f}",
                f"- macro_f1: {metrics['macro_f1']:.4f}",
                f"- present_labels_macro_f1: {metrics['present_labels_macro_f1']:.4f}",
                f"- parse_errors: {metrics['parse_errors']}",
                f"- confidence_normalized_count: {metrics['confidence_normalized_count']}",
                f"- high_risk_into_record: {metrics['high_risk_into_record']}",
                "",
                "### Per class",
                "",
                "| label | precision | recall | f1 |",
                "|---|---:|---:|---:|",
            ]
        )
        for label in LABELS:
            values = metrics["per_class"][label]
            lines.append(
                f"| {label} | {values['precision']:.4f} | {values['recall']:.4f} | {values['f1']:.4f} |"
            )
        lines.extend(["", "### Confusion matrix", "", "```json"])
        lines.append(json.dumps(metrics["confusion_matrix"], ensure_ascii=False, indent=2))
        lines.extend(["```", ""])
    lines.extend(
        render_stability(
            aggregate_stability(results), aggregate_usage(results), metadata["runs"]
        )
    )
    lines.extend(
        [
            "",
            "## Case results",
            "",
            "| model | run | case_id | expected | actual | verdict | trace_id |",
            "|---|---:|---|---|---|:---:|---|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result['model']} | {result['run_index']} | {result['case_id']} | "
            f"{result['expected']} | {result['actual']} | {result['verdict']} | "
            f"{result['trace_id']} |"
        )
    badcases = [result for result in results if not result["pass"]]
    lines.extend(["", "## Badcases", ""])
    if not badcases:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| model | run | case_id | input | expected | actual | verdict | 失败断言 | trace_id |",
                "|---|---:|---|---|---|---|:---:|---|---|",
            ]
        )
        for result in badcases:
            escaped_input = result["input"].replace("|", "\\|")
            lines.append(
                f"| {result['model']} | {result['run_index']} | {result['case_id']} | "
                f"{escaped_input} | {result['expected']} | {result['actual']} | "
                f"{result['verdict']} | {', '.join(result['failed_checks']) or '-'} | "
                f"{result['trace_id']} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--views", default="discovery", choices=["discovery", "locked", "regression", "all"])
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    add_matrix_args(parser)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    cases = load_cases(args.views, args.run_mode)
    if not cases:
        raise SystemExit("no cases selected")
    clients = build_clients(args.run_mode, args.models, args.temperature)
    before = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    all_results: list[dict[str, Any]] = []
    metrics_by_run: list[dict[str, Any]] = []
    for llm in clients:
        model_name = client_model(llm)
        for run_index in range(1, args.runs + 1):
            run_results = []
            for case in cases:
                result = run_case(case, args.run_mode, llm)
                result["run_index"] = run_index
                result["run_mode"] = args.run_mode
                result["model"] = model_name
                run_results.append(result)
            all_results.extend(run_results)
            metrics_by_run.append(
                {
                    "model": model_name,
                    "run_index": run_index,
                    "metrics": calculate_metrics(run_results),
                }
            )

    after = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    if before != after:
        raise SystemExit("unexpected business file write detected")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_path = RESULTS_DIR / f"case-results-{timestamp}.jsonl"
    report_path = RESULTS_DIR / f"report-{timestamp}.md"
    result_path.write_text(
        "\n".join(json.dumps(result, ensure_ascii=False) for result in all_results) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "view": args.views,
        "run_mode": args.run_mode,
        "model": ", ".join(client_model(llm) for llm in clients),
        "runs": args.runs,
        "git_commit": git_commit(),
        "prompt_hash": sha256_file(PROMPT_PATH),
        "dataset_hash": sha256_file(DATASET_PATH),
        "temperature": effective_temperature(args),
        "max_tokens": MAX_TOKENS,
        "thinking": THINKING_MODE,
        "timeout_seconds": TIMEOUT_SECONDS,
        "max_retries": MAX_RETRIES,
    }
    report_path.write_text(
        render_report(metadata, metrics_by_run, all_results), encoding="utf-8"
    )
    summary = {
        "result_path": str(result_path.relative_to(ROOT)),
        "report_path": str(report_path.relative_to(ROOT)),
        "metrics": metrics_by_run,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if all(result["pass"] for result in all_results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
