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


DATASET_PATH = ROOT / "eval" / "datasets" / "intent-dataset.jsonl"
RESULTS_DIR = ROOT / "eval" / "results"
TRACE_PATH = ROOT / "backend" / "logs" / "trace.jsonl"
LABELS = ["record", "query", "reject"]
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


def run_case(case: dict[str, Any], run_mode: str, live_llm: LiveLLM | None) -> dict[str, Any]:
    if run_mode == "stub":
        fault = case.get("inject_fault")
        raw_text = ""
        if not fault:
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
    if "expected_error_code" in case:
        assertion_pass = actual.get("error_code") == case["expected_error_code"]
    else:
        assertion_pass = actual.get("intent") == case["expected_intent"]
    event_names = {event["event"] for event in tracer.events}
    trace_pass = expected_trace_events(actual).issubset(event_names) and not tracer.write_failed
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
        "pass": assertion_pass and trace_pass,
        "trace_pass": trace_pass,
        "trace_id": actual["trace_id"],
        "error_code": actual.get("error_code"),
        "confidence_normalized": confidence_normalized,
    }


def safe_div(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def calculate_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    classified = [result for result in results if result["expected"] in LABELS]
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
    return {
        "total": len(results),
        "classified_total": len(classified),
        "passed": sum(1 for result in results if result["pass"]),
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
        f"- runs: {len(metrics_by_run)}",
        "",
        "> Stub results validate contracts only; they do not represent model quality."
        if metadata["run_mode"] == "stub"
        else "> Live results represent model behavior for this exact model, prompt and dataset snapshot.",
        "",
    ]
    for index, metrics in enumerate(metrics_by_run, start=1):
        lines.extend(
            [
                f"## Run {index}",
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
        [
            "## Case results",
            "",
            "| run | case_id | expected | actual | pass | trace_id |",
            "|---:|---|---|---|:---:|---|",
        ]
    )
    for result in results:
        lines.append(
            f"| {result['run_index']} | {result['case_id']} | {result['expected']} | "
            f"{result['actual']} | {'PASS' if result['pass'] else 'FAIL'} | {result['trace_id']} |"
        )
    badcases = [result for result in results if not result["pass"]]
    lines.extend(["", "## Badcases", ""])
    if not badcases:
        lines.append("None.")
    else:
        lines.extend(
            [
                "| run | case_id | input | expected | actual | trace_id |",
                "|---:|---|---|---|---|---|",
            ]
        )
        for result in badcases:
            escaped_input = result["input"].replace("|", "\\|")
            lines.append(
                f"| {result['run_index']} | {result['case_id']} | {escaped_input} | "
                f"{result['expected']} | {result['actual']} | {result['trace_id']} |"
            )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--views", default="discovery", choices=["discovery", "locked", "regression", "all"])
    parser.add_argument("--run-mode", default="stub", choices=["stub", "live"])
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be >= 1")

    load_dotenv()
    cases = load_cases(args.views, args.run_mode)
    if not cases:
        raise SystemExit("no cases selected")
    live_llm = LiveLLM() if args.run_mode == "live" else None
    before = snapshot([ROOT / "backend", ROOT / "eval" / "datasets"])
    all_results: list[dict[str, Any]] = []
    metrics_by_run: list[dict[str, Any]] = []
    for run_index in range(1, args.runs + 1):
        run_results = []
        for case in cases:
            result = run_case(case, args.run_mode, live_llm)
            result["run_index"] = run_index
            result["run_mode"] = args.run_mode
            run_results.append(result)
        all_results.extend(run_results)
        metrics_by_run.append(calculate_metrics(run_results))

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
        "model": live_llm.model if live_llm else "stub",
        "git_commit": git_commit(),
        "prompt_hash": sha256_file(PROMPT_PATH),
        "dataset_hash": sha256_file(DATASET_PATH),
        "temperature": TEMPERATURE,
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
