from __future__ import annotations

from pathlib import Path

from tools.check_moomoo_tasks import render_markdown, validate_task_records


def test_validate_task_records_accepts_expected_hidden_task(tmp_path: Path) -> None:
    script = tmp_path / "us_strategy" / "tick_collect.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooUSTickCollect",
            "Exists": True,
            "Arguments": (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{script}"'
            ),
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T21:00:00+08:00",
            "LastTaskResult": 0,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    us_tick = next(check for check in checks if check.task_name == "MoomooUSTickCollect")

    assert us_tick.status == "ok"
    assert us_tick.messages == ()


def test_validate_task_records_accepts_forward_collect_1600(tmp_path: Path) -> None:
    script = tmp_path / "us_strategy" / "forward_collect.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooForwardCollect",
            "Exists": True,
            "Arguments": (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{script}"'
            ),
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T16:00:00+08:00",
            "ExecutionTimeLimit": "PT17H",
            "LastTaskResult": 0,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    forward = next(check for check in checks if check.task_name == "MoomooForwardCollect")

    assert forward.status == "ok"
    assert forward.messages == ()
    assert forward.actual_execution_time_limit == "PT17H"


def test_validate_task_records_flags_missing_hidden_argument(tmp_path: Path) -> None:
    script = tmp_path / "hk_strategy" / "tick_collect.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooHKTickCollect",
            "Exists": True,
            "Arguments": f'-NoProfile -ExecutionPolicy Bypass -File "{script}"',
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T09:15:00+08:00",
            "LastTaskResult": 0,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    hk_tick = next(check for check in checks if check.task_name == "MoomooHKTickCollect")

    assert hk_tick.status == "warn"
    assert "action is not hidden" in hk_tick.messages
    assert "Moomoo Scheduled Task Check" in render_markdown([hk_tick])


def test_validate_task_records_accepts_us_sim_trade_task(tmp_path: Path) -> None:
    script = tmp_path / "us_strategy" / "run_simulate.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooUSSimTrade",
            "Exists": True,
            "Arguments": (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{script}"'
            ),
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T21:15:00+08:00",
            "LastTaskResult": 0,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    sim_trade = next(check for check in checks if check.task_name == "MoomooUSSimTrade")

    assert sim_trade.status == "ok"
    assert sim_trade.messages == ()


def test_validate_task_records_accepts_running_task_result(tmp_path: Path) -> None:
    script = tmp_path / "us_strategy" / "run_simulate.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooUSSimTrade",
            "Exists": True,
            "Arguments": (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{script}"'
            ),
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T21:15:00+08:00",
            "LastTaskResult": 267009,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    sim_trade = next(check for check in checks if check.task_name == "MoomooUSSimTrade")

    assert sim_trade.status == "ok"
    assert sim_trade.messages == ()


def test_validate_task_records_accepts_hk_sim_trade_task(tmp_path: Path) -> None:
    script = tmp_path / "hk_strategy" / "run_simulate_task.ps1"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    records = [
        {
            "TaskName": "MoomooHKSimTrade",
            "Exists": True,
            "Arguments": (
                '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden '
                f'-File "{script}"'
            ),
            "TriggerType": "MSFT_TaskWeeklyTrigger",
            "StartBoundary": "2026-06-08T09:15:00+08:00",
            "LastTaskResult": 0,
        }
    ]

    checks = validate_task_records(records, tmp_path)
    sim_trade = next(check for check in checks if check.task_name == "MoomooHKSimTrade")

    assert sim_trade.status == "ok"
    assert sim_trade.messages == ()
