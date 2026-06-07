from __future__ import annotations

import argparse
import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


EXPECTED_TASKS: tuple[dict[str, Any], ...] = (
    {
        "task_name": "MoomooForwardCollect",
        "script_path": "us_strategy\\forward_collect.ps1",
        "trigger_type": "weekly",
        "time": "21:00",
    },
    {
        "task_name": "MoomooICReport",
        "script_path": "us_strategy\\ic_report.ps1",
        "trigger_type": "weekly",
        "time": "06:30",
    },
    {
        "task_name": "MoomooHKForwardCollect",
        "script_path": "hk_strategy\\forward_collect.ps1",
        "trigger_type": "weekly",
        "time": "09:15",
    },
    {
        "task_name": "MoomooHKICReport",
        "script_path": "hk_strategy\\ic_report.ps1",
        "trigger_type": "weekly",
        "time": "16:30",
    },
    {
        "task_name": "MoomooUSDailyWatchlistBackfill",
        "script_path": "us_strategy\\daily_watchlist_backfill.ps1",
        "trigger_type": "daily",
        "time": "06:30",
    },
    {
        "task_name": "MoomooHKTickCollect",
        "script_path": "hk_strategy\\tick_collect.ps1",
        "trigger_type": "weekly",
        "time": "09:15",
    },
    {
        "task_name": "MoomooUSTickCollect",
        "script_path": "us_strategy\\tick_collect.ps1",
        "trigger_type": "weekly",
        "time": "21:00",
    },
)


@dataclass(frozen=True)
class TaskCheck:
    """One read-only scheduled task validation result."""

    task_name: str
    status: str
    script_path: str
    trigger_type: str
    expected_time: str
    actual_time: str | None
    last_task_result: int | None
    messages: tuple[str, ...]


def validate_task_records(
    records: list[dict[str, Any]],
    root: Path,
) -> list[TaskCheck]:
    """Validate scheduled task records returned by PowerShell."""

    by_name = {str(row.get("TaskName")): row for row in records}
    checks: list[TaskCheck] = []
    for expected in EXPECTED_TASKS:
        name = str(expected["task_name"])
        script_path = str(expected["script_path"])
        trigger_type = str(expected["trigger_type"])
        expected_time = str(expected["time"])
        record = by_name.get(name)
        messages: list[str] = []
        if record is None or not record.get("Exists", True):
            checks.append(
                TaskCheck(
                    task_name=name,
                    status="missing",
                    script_path=script_path,
                    trigger_type=trigger_type,
                    expected_time=expected_time,
                    actual_time=None,
                    last_task_result=None,
                    messages=("scheduled task is missing",),
                )
            )
            continue

        arguments = str(record.get("Arguments") or "")
        actual_time = _time_from_boundary(record.get("StartBoundary"))
        actual_trigger = str(record.get("TriggerType") or "").lower()
        last_result = _int_or_none(record.get("LastTaskResult"))
        absolute_script = root / script_path

        if script_path.lower() not in arguments.lower():
            messages.append("action does not point to the expected script")
        if "-windowstyle hidden" not in arguments.lower():
            messages.append("action is not hidden")
        if not absolute_script.exists():
            messages.append("script file is missing")
        if trigger_type not in actual_trigger:
            messages.append(f"trigger type mismatch: {actual_trigger or 'unknown'}")
        if actual_time != expected_time:
            messages.append(f"time mismatch: {actual_time or 'unknown'}")
        if last_result not in (None, 0, 267011, 267014):
            messages.append(f"last task result is abnormal: {last_result}")

        checks.append(
            TaskCheck(
                task_name=name,
                status="ok" if not messages else "warn",
                script_path=script_path,
                trigger_type=trigger_type,
                expected_time=expected_time,
                actual_time=actual_time,
                last_task_result=last_result,
                messages=tuple(messages),
            )
        )
    return checks


def fetch_windows_task_records() -> list[dict[str, Any]]:
    """Read Moomoo scheduled task metadata from Windows Task Scheduler."""

    if platform.system().lower() != "windows":
        raise RuntimeError("scheduled task validation requires Windows")

    names = ",".join(f"'{task['task_name']}'" for task in EXPECTED_TASKS)
    script = f"""
$names = @({names})
$rows = foreach ($name in $names) {{
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {{
        [PSCustomObject]@{{ TaskName = $name; Exists = $false }}
        continue
    }}
    $info = Get-ScheduledTaskInfo -TaskName $name
    $trigger = $task.Triggers | Select-Object -First 1
    [PSCustomObject]@{{
        TaskName = $task.TaskName
        Exists = $true
        State = [string]$task.State
        Execute = [string]$task.Actions.Execute
        Arguments = [string]$task.Actions.Arguments
        TriggerType = [string]$trigger.CimClass.CimClassName
        StartBoundary = [string]$trigger.StartBoundary
        LastTaskResult = [int]$info.LastTaskResult
        LastRunTime = [string]$info.LastRunTime
        NextRunTime = [string]$info.NextRunTime
    }}
}}
$rows | ConvertTo-Json -Depth 6
"""
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "Get-ScheduledTask failed")
    payload = completed.stdout.strip()
    if not payload:
        return []
    data = json.loads(payload)
    if isinstance(data, dict):
        return [data]
    return list(data)


def render_markdown(checks: list[TaskCheck]) -> str:
    """Render task validation results as Markdown."""

    lines = [
        "# Moomoo Scheduled Task Check",
        "",
        "| task | status | expected | actual | messages |",
        "|---|---|---|---|---|",
    ]
    for check in checks:
        messages = "; ".join(check.messages) if check.messages else "ok"
        expected = f"{check.trigger_type} {check.expected_time}"
        actual = check.actual_time or "unknown"
        lines.append(
            f"| `{check.task_name}` | {check.status} | {expected} | "
            f"{actual} | {messages} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    """Validate local Windows scheduled tasks for Moomoo data automation."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    records = fetch_windows_task_records()
    checks = validate_task_records(records, Path(args.root).resolve())
    if args.json:
        print(json.dumps([asdict(check) for check in checks], ensure_ascii=False, indent=2))
    else:
        print(render_markdown(checks), end="")
    if args.strict and any(check.status != "ok" for check in checks):
        return 1
    return 0


def _time_from_boundary(value: Any) -> str | None:
    raw = str(value or "")
    if "T" not in raw:
        return None
    return raw.split("T", 1)[1][:5]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
