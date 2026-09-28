import pytest

from eval.run_regression import parse_runner_output, render


def test_parse_runner_output_reads_last_json_line_and_fault_injection():
    stdout = "进度日志\n" + (
        '{"report_path": "eval/results/plan-report.md", "metrics": [{"model": "stub", "run_index": 1, '
        '"metrics": {"total": 8, "verdicts": {"PASS": 7, "REVIEW": 1}, '
        '"fault_injection_total": 2, "fault_injection_passed": 2}}]}'
    )
    row = parse_runner_output(stdout)
    assert row == {"total": 8, "report": "eval/results/plan-report.md",
                   "PASS": 7, "REVIEW": 1, "FAIL": 0, "ERROR": 0, "fault": "2/2"}


def test_parse_runner_output_without_json_raises():
    with pytest.raises(ValueError):
        parse_runner_output("Traceback: boom")


def test_render_marks_failure_when_any_runner_exit_nonzero():
    row = {"total": 1, "PASS": 0, "REVIEW": 0, "FAIL": 1, "ERROR": 0, "exit": 1}
    table = render({"passed": 3, "failed": 0, "exit": 0}, [("意图识别", "run_intent_eval.py", row)])
    assert "| 意图识别 | `run_intent_eval.py` | 1 | 0 | 0 | 1 | 0 | - | 1 |" in table
    assert table.endswith("有失败项，查看对应 Runner 的报告")
