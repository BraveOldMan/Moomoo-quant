# -*- coding: utf-8 -*-
"""Utilities for persisting same-day IPO watchlists."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class IpoWatchRecord:
    """One IPO watchlist row.

    The file format is intentionally small and stable:
    YYYY-MM-DD<TAB>CODE<TAB>NAME<TAB>LIST_TIME
    """

    trade_date: date
    code: str
    name: str
    list_time: str

    def line(self) -> str:
        """Render this record as one UTF-8 text line."""

        return "\t".join(
            (
                self.trade_date.isoformat(),
                self.code,
                self.name.replace("\t", " ").strip(),
                self.list_time.replace("\t", " ").strip(),
            )
        )


def load_today_records(path: str | Path, target_date: date) -> dict[str, IpoWatchRecord]:
    """Load only target_date records from the IPO watchlist file."""

    records: dict[str, IpoWatchRecord] = {}
    file_path = Path(path)
    if not file_path.exists():
        return records

    for line in file_path.read_text(encoding="utf-8-sig").splitlines():
        record = _parse_line(line)
        if record is None or record.trade_date != target_date:
            continue
        records[record.code] = record
    return records


def append_today_records(
    path: str | Path,
    records: dict[str, IpoWatchRecord],
) -> list[IpoWatchRecord]:
    """Append missing IPO records while preserving existing historical rows."""

    if not records:
        return []

    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines: list[str] = []
    seen_keys: set[tuple[str, str]] = set()
    if file_path.exists():
        for line in file_path.read_text(encoding="utf-8-sig").splitlines():
            existing_lines.append(line)
            record = _parse_line(line)
            if record is not None:
                seen_keys.add((record.trade_date.isoformat(), record.code))

    added: list[IpoWatchRecord] = []
    for record in sorted(records.values(), key=lambda item: item.code):
        key = (record.trade_date.isoformat(), record.code)
        if key in seen_keys:
            continue
        existing_lines.append(record.line())
        seen_keys.add(key)
        added.append(record)

    if added:
        file_path.write_text(
            "\n".join(existing_lines).rstrip() + "\n",
            encoding="utf-8",
        )
    return added


def _parse_line(line: str) -> IpoWatchRecord | None:
    text = line.strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split("\t")
    if len(parts) != 4:
        return None
    try:
        trade_date = date.fromisoformat(parts[0])
    except ValueError:
        return None
    code = parts[1].strip()
    if not code:
        return None
    return IpoWatchRecord(
        trade_date=trade_date,
        code=code,
        name=parts[2].strip() or code,
        list_time=parts[3].strip() or trade_date.isoformat(),
    )
