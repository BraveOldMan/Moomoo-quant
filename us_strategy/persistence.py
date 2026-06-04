# -*- coding: utf-8 -*-
"""SQLite 持久化：进程重启后恢复持仓状态（含加权成本所需的 qty）。"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class PositionRecord:
    code: str
    cost_price: float  # 加权平均成本
    buy_date: date
    tranches_bought: int
    peak_price: float
    qty: float = 0.0


class PositionStore:
    def __init__(self, db_path: str = "us_strategy/positions.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    code            TEXT PRIMARY KEY,
                    cost_price      REAL NOT NULL,
                    buy_date        TEXT NOT NULL,
                    tranches_bought INTEGER NOT NULL DEFAULT 1,
                    peak_price      REAL NOT NULL,
                    qty             REAL NOT NULL DEFAULT 0
                )
            """)
            # 旧库迁移：补 qty 列
            cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
            if "qty" not in cols:
                conn.execute(
                    "ALTER TABLE positions ADD COLUMN qty REAL NOT NULL DEFAULT 0"
                )

    def save(self, record: PositionRecord) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO positions
                    (code, cost_price, buy_date, tranches_bought, peak_price, qty)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    cost_price      = excluded.cost_price,
                    buy_date        = excluded.buy_date,
                    tranches_bought = excluded.tranches_bought,
                    peak_price      = excluded.peak_price,
                    qty             = excluded.qty
            """,
                (
                    record.code,
                    record.cost_price,
                    record.buy_date.isoformat(),
                    record.tranches_bought,
                    record.peak_price,
                    record.qty,
                ),
            )

    def delete(self, code: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM positions WHERE code = ?", (code,))

    def load_all(self) -> dict[str, PositionRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT code, cost_price, buy_date, tranches_bought, peak_price, qty"
                " FROM positions"
            ).fetchall()
        return {
            row[0]: PositionRecord(
                code=row[0],
                cost_price=row[1],
                buy_date=date.fromisoformat(row[2]),
                tranches_bought=row[3],
                peak_price=row[4],
                qty=row[5],
            )
            for row in rows
        }


@dataclass
class SignalLogRecord:
    ts: str  # ISO8601 UTC 时间戳
    code: str
    last_price: float
    scores: dict[str, float]  # 各因子风险分快照


class SignalLogStore:
    """前向日志：落库每次评分的因子分 + 当时价格。

    微观结构因子（CVD/OBI）无历史回放，唯一可信的校准方式是前向收集——
    记录 (T 时刻因子分, 价格)，待 T+N 真实收益出现后用 analysis 的前向 IC 评估。
    与 PositionStore 共用同一 SQLite 文件（不同表），复用连接模式。
    """

    def __init__(self, db_path: str = "us_strategy/positions.db"):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_log (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts         TEXT NOT NULL,
                    code       TEXT NOT NULL,
                    last_price REAL NOT NULL,
                    scores     TEXT NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_signal_log_code ON signal_log(code, ts)"
            )

    def log(self, code: str, last_price: float, scores: dict[str, float]) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO signal_log (ts, code, last_price, scores) VALUES (?,?,?,?)",
                (ts, code, last_price, json.dumps(scores)),
            )

    def load(self, code: str | None = None) -> list[SignalLogRecord]:
        sql = "SELECT ts, code, last_price, scores FROM signal_log"
        params: tuple = ()
        if code is not None:
            sql += " WHERE code = ?"
            params = (code,)
        sql += " ORDER BY ts"
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SignalLogRecord(
                ts=r[0], code=r[1], last_price=r[2], scores=json.loads(r[3])
            )
            for r in rows
        ]
