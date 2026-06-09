from __future__ import annotations

import argparse
import json
import math
import shutil
import sqlite3
import subprocess
import sys
import time as time_module
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from moomoo_rate_limits import DEFAULT_OPTION_CHAIN_SLEEP_SECONDS
import us_strategy.signals as signals_mod
import us_strategy.strategy as strategy_mod
from us_strategy import features
from us_strategy.config import Signal, StrategyConfig
from us_strategy.data_access import DataAccess
from us_strategy.market_calendar import is_trading_day
from us_strategy.persistence import PositionRecord
from us_strategy.signals import SignalCalculator, SignalResult
from us_strategy.strategy import IPOStrategy


DEFAULT_CHAT_ID = "oc_bc9a36b4392dbe632fb4e50a3ef7ef17"
DEFAULT_WATCHLIST = Path("us_strategy/watchlist.txt")
DEFAULT_OUTPUT_ROOT = Path("report/outputs/us_daily")
US_TZ = ZoneInfo("America/New_York")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
MARKET_CLOSE = time(16, 0)
REPORT_DELAY_MINUTES = 30
OPTION_API_MAX_ATTEMPTS = 3
OPTION_API_RETRY_DELAY_SECONDS = 1.0
OPTION_API_SUCCESS_PAUSE_SECONDS = DEFAULT_OPTION_CHAIN_SLEEP_SECONDS

INDEX_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("S&P 500", ("US..SPX", "US.SPY")),
    ("Nasdaq", ("US..IXIC", "US.QQQ")),
    ("Dow Jones", ("US..DJI", "US.DIA")),
    ("Russell 2000", ("US..RUT", "US.IWM")),
    ("Volatility", ("US..VIX", "US.VIXY")),
)


class QuoteContext(Protocol):
    """Read-only subset of moomoo OpenQuoteContext used by this report."""

    def get_market_snapshot(self, code_list: list[str]) -> tuple[Any, Any]:
        """Return a market snapshot for one or more symbols."""

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype: Any = ft.KLType.K_DAY,
        max_count: int = 100,
    ) -> tuple[Any, Any, Any]:
        """Return historical kline rows."""

    def get_capital_distribution(self, code: str) -> tuple[Any, Any]:
        """Return capital distribution rows."""

    def get_option_expiration_date(self, code: str) -> tuple[Any, Any]:
        """Return option expiration rows."""

    def get_option_chain(self, code: str, start: str, end: str) -> tuple[Any, Any]:
        """Return option chain rows."""

    def get_daily_short_volume(self, code: str) -> tuple[Any, Any]:
        """Return daily short volume rows."""

    def get_short_interest(self, code: str) -> tuple[Any, Any]:
        """Return short interest rows."""


@dataclass(frozen=True)
class DailyBar:
    """Daily OHLCV bar anchored to one market date."""

    code: str
    trade_date: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    change_rate_pct: float | None
    turnover: float | None
    turnover_rate: float | None
    volume: float | None
    source_code: str
    source_kind: str


@dataclass(frozen=True)
class PositionSnapshot:
    """Local persisted position state used by the main strategy stop logic."""

    has_position: bool
    qty: float | None
    cost_price: float | None
    buy_date: str | None
    tranches_bought: int
    peak_price: float | None
    unrealized_return: float | None
    drawdown_from_peak: float | None


@dataclass(frozen=True)
class ReportPaths:
    """Output paths produced by one daily report run."""

    output_dir: Path
    summary_md: Path
    report_json: Path
    stock_csv: Path
    option_csv: Path
    lark_create_json: Path
    lark_send_json: Path
    lark_fetch_json: Path
    lark_message_json: Path
    lark_card_json: Path
    lark_send_body_json: Path
    lark_send_params_json: Path
    failure_json: Path


@dataclass(frozen=True)
class LarkResult:
    """Feishu send and readback result."""

    doc_url: str | None
    file_token: str | None
    message_id: str | None
    skipped: bool


class ReportError(RuntimeError):
    """Raised when the daily report must fail closed."""


class ReadOnlyTradeContext:
    """Trade context stub so DataAccess cannot query account or orders."""

    def position_list_query(self, *args: Any, **kwargs: Any) -> tuple[int, str]:
        """Reject account position access."""

        return ft.RET_ERROR, "read-only report does not query trade positions"

    def accinfo_query(self, *args: Any, **kwargs: Any) -> tuple[int, str]:
        """Reject account cash access."""

        return ft.RET_ERROR, "read-only report does not query account info"


def load_us_watchlist(path: Path) -> tuple[str, ...]:
    """Load deduplicated US symbols from a watchlist text file."""

    if not path.exists():
        return ()
    raw_codes: list[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            clean = line.split("#", 1)[0].strip()
            if not clean:
                continue
            raw_codes.extend(part.strip() for part in clean.split(","))

    seen: set[str] = set()
    codes: list[str] = []
    for code in raw_codes:
        if code.startswith("US.") and code not in seen:
            seen.add(code)
            codes.append(code)
    return tuple(codes)


def infer_target_date(
    now: datetime | None = None,
    explicit_date: date | None = None,
) -> date:
    """Infer the US market date that the after-close report should analyze."""

    if explicit_date is not None:
        return explicit_date
    current = now or datetime.now(US_TZ)
    local_now = current.astimezone(US_TZ)
    return local_now.date()


def validate_target_date(
    target_date: date,
    now: datetime | None,
    explicit_date: bool,
    force: bool,
) -> None:
    """Fail closed unless the target US trading date is ready for reporting."""

    if not is_trading_day(target_date):
        raise ReportError(f"{target_date.isoformat()} is not a US trading day")
    if explicit_date or force:
        return
    local_now = (now or datetime.now(US_TZ)).astimezone(US_TZ)
    ready_at = datetime.combine(target_date, MARKET_CLOSE, tzinfo=US_TZ) + timedelta(
        minutes=REPORT_DELAY_MINUTES,
    )
    if local_now < ready_at:
        raise ReportError(
            "US market after-close data is not ready: "
            f"now={local_now.isoformat()} ready_at={ready_at.isoformat()}",
        )


def build_paths(target_date: date, output_dir: Path | None) -> ReportPaths:
    """Build all deterministic output paths for one report date."""

    out = (
        output_dir if output_dir is not None else DEFAULT_OUTPUT_ROOT / ymd(target_date)
    )
    return ReportPaths(
        output_dir=out,
        summary_md=out / "summary.md",
        report_json=out / "report.json",
        stock_csv=out / "stock_factors.csv",
        option_csv=out / "options.csv",
        lark_create_json=out / "lark_create.json",
        lark_send_json=out / "lark_send.json",
        lark_fetch_json=out / "lark_fetch.json",
        lark_message_json=out / "lark_message.json",
        lark_card_json=out / "lark_card.json",
        lark_send_body_json=out / "lark_send_body.json",
        lark_send_params_json=out / "lark_send_params.json",
        failure_json=out / "failure.json",
    )


def ymd(value: date) -> str:
    """Format a date as YYYYMMDD."""

    return value.strftime("%Y%m%d")


def safe_float(value: Any) -> float | None:
    """Convert numeric-like values to finite float."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def extract_trade_date(row: pd.Series) -> str:
    """Extract YYYY-MM-DD from a moomoo kline row."""

    raw = row.get("time_key", row.get("date", ""))
    return str(raw)[:10]


def fetch_daily_bar(
    data: DataAccess,
    code: str,
    target_date: date,
    source_kind: str,
    source_code: str | None = None,
) -> tuple[DailyBar | None, str | None]:
    """Fetch a single daily bar for code on target_date."""

    query_code = source_code or code
    start = (target_date - timedelta(days=90)).isoformat()
    end = target_date.isoformat()
    try:
        ret, frame, _ = data.request_history_kline(
            query_code,
            start=start,
            end=end,
            ktype=ft.KLType.K_DAY,
            max_count=90,
        )
    except Exception as exc:  # noqa: BLE001 - preserved in report diagnostics.
        return None, f"{query_code} history exception: {exc}"
    if ret != ft.RET_OK:
        return None, f"{query_code} history error: {frame}"
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return None, f"{query_code} history empty"

    dated = frame.copy()
    dated["__date"] = dated.apply(extract_trade_date, axis=1)
    matched = dated[dated["__date"] == target_date.isoformat()]
    if matched.empty:
        return None, f"{query_code} missing daily bar for {target_date.isoformat()}"
    row = matched.iloc[-1]
    return (
        DailyBar(
            code=code,
            trade_date=extract_trade_date(row),
            open=safe_float(row.get("open")),
            high=safe_float(row.get("high")),
            low=safe_float(row.get("low")),
            close=safe_float(row.get("close")),
            change_rate_pct=safe_float(row.get("change_rate")),
            turnover=safe_float(row.get("turnover")),
            turnover_rate=safe_float(row.get("turnover_rate")),
            volume=safe_float(row.get("volume")),
            source_code=query_code,
            source_kind=source_kind,
        ),
        None,
    )


def fetch_index_bars(
    data: DataAccess, target_date: date
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch index daily bars with ETF fallback proxies."""

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for label, candidates in INDEX_GROUPS:
        selected: DailyBar | None = None
        selected_error: str | None = None
        for index, candidate in enumerate(candidates):
            source_kind = "index" if index == 0 else "etf_proxy"
            bar, err = fetch_daily_bar(data, label, target_date, source_kind, candidate)
            if bar is not None and bar.close is not None:
                selected = bar
                selected_error = None
                break
            selected_error = err
        if selected is None:
            errors.append(selected_error or f"{label} no available index or proxy bar")
            continue
        rows.append(
            {
                "label": label,
                "source_code": selected.source_code,
                "source_kind": selected.source_kind,
                "trade_date": selected.trade_date,
                "close": selected.close,
                "change_rate_pct": selected.change_rate_pct,
                "turnover": selected.turnover,
                "volume": selected.volume,
            },
        )
    return rows, errors


def position_snapshot(
    record: PositionRecord | None,
    close_price: float | None,
) -> PositionSnapshot:
    """Convert a persisted position record into report metrics."""

    if record is None:
        return PositionSnapshot(False, None, None, None, 0, None, None, None)
    unrealized = None
    drawdown = None
    if close_price is not None and record.cost_price > 0:
        unrealized = close_price / record.cost_price - 1.0
    if close_price is not None and record.peak_price > 0:
        drawdown = 1.0 - close_price / record.peak_price
    return PositionSnapshot(
        has_position=True,
        qty=record.qty,
        cost_price=record.cost_price,
        buy_date=record.buy_date.isoformat(),
        tranches_bought=record.tranches_bought,
        peak_price=record.peak_price,
        unrealized_return=unrealized,
        drawdown_from_peak=drawdown,
    )


def load_positions(db_path: str) -> dict[str, PositionRecord]:
    """Load local persisted positions without creating a missing database."""

    path = Path(db_path)
    if not path.exists():
        return {}
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as conn:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'positions'",
        ).fetchone()
        if table_exists is None:
            return {}
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(positions)").fetchall()
        }
        optional_cols = [
            "qty" if "qty" in cols else "0 AS qty",
            "origin" if "origin" in cols else "'regular' AS origin",
        ]
        rows = conn.execute(
            "SELECT code, cost_price, buy_date, tranches_bought, peak_price, "
            + ", ".join(optional_cols)
            + " FROM positions",
        ).fetchall()
    return {
        row[0]: PositionRecord(
            code=row[0],
            cost_price=row[1],
            buy_date=date.fromisoformat(row[2]),
            tranches_bought=row[3],
            peak_price=row[4],
            qty=row[5],
            origin=row[6] or "regular",
        )
        for row in rows
    }


@contextmanager
def anchor_strategy_date(target_date: date) -> Iterator[None]:
    """Temporarily anchor strategy date helpers to target_date."""

    old_signals_date = signals_mod.market_date
    old_strategy_date = strategy_mod.market_date
    signals_mod.market_date = lambda timezone_name: target_date
    strategy_mod.market_date = lambda timezone_name: target_date
    try:
        yield
    finally:
        signals_mod.market_date = old_signals_date
        strategy_mod.market_date = old_strategy_date


def make_diagnostic_config(config: StrategyConfig) -> StrategyConfig:
    """Enable optional diagnostics without changing the main strategy config."""

    from dataclasses import replace

    return replace(
        config,
        use_rs=True,
        use_short_metrics=True,
        use_option_iv=True,
        use_macro_filter=True,
        use_crypto_filter=True,
    )


def analyze_stocks(
    data: DataAccess,
    config: StrategyConfig,
    codes: tuple[str, ...],
    target_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Analyze watchlist stocks with the main strategy and diagnostic factors."""

    main_calc = SignalCalculator(data, config, signal_log=None)
    diag_calc = SignalCalculator(data, make_diagnostic_config(config), signal_log=None)
    strategy = IPOStrategy(main_calc, config)
    positions = load_positions(config.db_path)
    for code, record in positions.items():
        strategy.restore_position(
            code=code,
            avg_cost=record.cost_price,
            qty=record.qty,
            buy_date=record.buy_date,
            tranches_bought=record.tranches_bought,
            peak_price=record.peak_price,
            origin=record.origin,
        )

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for code in codes:
        bar, bar_error = fetch_daily_bar(data, code, target_date, "stock")
        if bar_error:
            errors.append(bar_error)
        close_price = bar.close if bar else None
        if close_price is None:
            rows.append(
                {
                    "code": code,
                    "signal": Signal.HOLD.value,
                    "score": 50.0,
                    "reason": "daily close missing",
                    "bar": daily_bar_dict(bar),
                    "core_scores": {},
                    "diagnostic_scores": {},
                    "diagnostic_extra": {},
                    "factor_gaps": ["daily_close"],
                    "liquidity_ok": False,
                    "risk_warnings": [],
                    "buy_block_reasons": [],
                    "position": position_snapshot(
                        positions.get(code), close_price
                    ).__dict__,
                    "factor_note": "daily bar missing",
                },
            )
            continue

        decision = strategy.evaluate(code, current_price=close_price)
        main_result = main_calc.calculate(code, last_price=close_price)
        diag_result = diag_calc.calculate(code, last_price=close_price)
        result = main_result or diag_result
        gaps = factor_gaps(diag_result)
        rows.append(
            {
                "code": code,
                "signal": decision.signal.value,
                "score": decision.score,
                "reason": decision.reason,
                "bar": daily_bar_dict(bar),
                "atr": decision.atr,
                "core_scores": core_scores(main_result),
                "diagnostic_scores": dict(diag_result.scores) if diag_result else {},
                "diagnostic_extra": dict(diag_result.extra) if diag_result else {},
                "factor_gaps": gaps,
                "liquidity_ok": result.liquidity_ok if result else False,
                "risk_warnings": list(result.risk_warnings) if result else [],
                "buy_block_reasons": list(result.buy_block_reasons) if result else [],
                "position": position_snapshot(
                    positions.get(code), close_price
                ).__dict__,
                "factor_note": factor_note(diag_result or main_result),
            },
        )
    rows.sort(key=lambda row: signal_sort_key(row["signal"], row["score"], row["code"]))
    return rows, errors


def signal_sort_key(signal: str, score: float, code: str) -> tuple[int, float, str]:
    """Sort actionable rows first for the report."""

    order = {Signal.BUY.value: 0, Signal.SELL.value: 1, Signal.HOLD.value: 2}
    return order.get(signal, 9), score, code


def daily_bar_dict(bar: DailyBar | None) -> dict[str, Any]:
    """Serialize a DailyBar into a plain dictionary."""

    if bar is None:
        return {}
    return {
        "code": bar.code,
        "trade_date": bar.trade_date,
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "change_rate_pct": bar.change_rate_pct,
        "turnover": bar.turnover,
        "turnover_rate": bar.turnover_rate,
        "volume": bar.volume,
        "source_code": bar.source_code,
        "source_kind": bar.source_kind,
    }


def core_scores(result: SignalResult | None) -> dict[str, float | None]:
    """Return stable core factor columns for stock CSV output."""

    scores = result.scores if result else {}
    return {
        "turnover": scores.get("turnover"),
        "capital": scores.get("capital"),
        "momentum": scores.get("momentum"),
    }


def factor_gaps(result: SignalResult | None) -> list[str]:
    """List unavailable factor groups for one stock."""

    if result is None:
        return ["signal_result"]
    expected = (
        "turnover",
        "capital",
        "momentum",
        "rs",
        "short",
        "option_iv",
        "macro_filter",
    )
    return [name for name in expected if name not in result.scores]


def factor_note(result: SignalResult | None) -> str:
    """Summarize the strongest available factor signals."""

    if result is None or not result.scores:
        return "数据不足"
    sorted_scores = sorted(result.scores.items(), key=lambda item: item[1])
    low = [f"{key}:{value:.1f}" for key, value in sorted_scores if value < 40]
    high = [f"{key}:{value:.1f}" for key, value in sorted_scores if value >= 60]
    if low and high:
        return "多头 " + ", ".join(low[:2]) + "; 风险 " + ", ".join(high[:2])
    if low:
        return "多头 " + ", ".join(low[:3])
    if high:
        return "风险 " + ", ".join(high[:3])
    return "中性 " + ", ".join(f"{key}:{value:.1f}" for key, value in sorted_scores[:3])


def analyze_options(
    ctx: QuoteContext,
    codes: tuple[str, ...],
    stock_rows: list[dict[str, Any]],
    target_date: date,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Analyze limited-depth options for each watchlist code."""

    close_by_code = {
        row["code"]: row.get("bar", {}).get("close")
        for row in stock_rows
        if row.get("bar", {}).get("close") is not None
    }
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for code in codes:
        latest_close = safe_float(close_by_code.get(code))
        ret, exp = option_quote_call(lambda c=code: ctx.get_option_expiration_date(c))
        if ret != ft.RET_OK or not isinstance(exp, pd.DataFrame) or exp.empty:
            if ret != ft.RET_OK:
                errors.append(
                    f"{code} option_expiration unavailable: {describe_api_error(ret, exp)}"
                )
            rows.append(option_gap_row(code, target_date, "no_option_expiration"))
            continue
        expiries = select_expiries(exp, 2)
        if not expiries:
            rows.append(option_gap_row(code, target_date, "no_expiry_within_table"))
            continue
        chains, chain_errors = fetch_option_chains(ctx, code, expiries)
        errors.extend(f"{code} option_chain {error}" for error in chain_errors)
        for expiry in expiries:
            chain = chains.get(expiry)
            if chain is None or chain.empty:
                rows.append(
                    option_gap_row(code, target_date, f"chain_missing:{expiry}")
                )
                continue
            pair = select_atm_pair(chain, latest_close)
            if pair is None:
                rows.append(
                    option_gap_row(code, target_date, f"atm_pair_missing:{expiry}")
                )
                continue
            rows.append(
                option_pair_row(ctx, code, expiry, pair, latest_close, target_date)
            )
    return rows, errors


def safe_quote_call(func: Any) -> tuple[Any, Any]:
    """Run a quote API call and convert exceptions into RET_ERROR."""

    try:
        return func()
    except Exception as exc:  # noqa: BLE001 - API errors are report diagnostics.
        return ft.RET_ERROR, str(exc)


def option_quote_call(func: Any) -> tuple[Any, Any]:
    """Run an option quote API call with light pacing and retry on transient errors."""

    result: tuple[Any, Any] = (ft.RET_ERROR, "not_called")
    for attempt in range(OPTION_API_MAX_ATTEMPTS):
        result = safe_quote_call(func)
        if result[0] == ft.RET_OK:
            if OPTION_API_SUCCESS_PAUSE_SECONDS > 0:
                time_module.sleep(OPTION_API_SUCCESS_PAUSE_SECONDS)
            return result
        if attempt < OPTION_API_MAX_ATTEMPTS - 1 and OPTION_API_RETRY_DELAY_SECONDS > 0:
            # 指数退避（1x, 2x, 4x ...）：对限频/超时比线性更友好。
            time_module.sleep(OPTION_API_RETRY_DELAY_SECONDS * (2**attempt))
    return result


def describe_api_error(ret_code: Any, data: Any) -> str:
    """Return a compact diagnostic string for a failed moomoo API response."""

    if isinstance(data, str):
        return data[:160]
    if isinstance(data, pd.DataFrame):
        return f"ret={ret_code}, rows={len(data)}, columns={list(data.columns)[:8]}"
    return f"ret={ret_code}, data_type={type(data).__name__}"


def fetch_option_chains(
    ctx: QuoteContext,
    code: str,
    expiries: list[str],
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Fetch option chains by expiry, preferring one range request per underlying."""

    chains: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    if not expiries:
        return chains, errors

    ret, frame = option_quote_call(
        lambda c=code, start=expiries[0], end=expiries[-1]: ctx.get_option_chain(
            c,
            start=start,
            end=end,
        ),
    )
    if ret == ft.RET_OK and isinstance(frame, pd.DataFrame) and not frame.empty:
        chains.update(split_option_chain_by_expiry(frame, expiries))
    else:
        errors.append(
            f"{expiries[0]}..{expiries[-1]} unavailable: {describe_api_error(ret, frame)}"
        )

    missing = [
        expiry for expiry in expiries if expiry not in chains or chains[expiry].empty
    ]
    for expiry in missing:
        ret, frame = option_quote_call(
            lambda c=code, ex=expiry: ctx.get_option_chain(c, start=ex, end=ex),
        )
        if ret == ft.RET_OK and isinstance(frame, pd.DataFrame) and not frame.empty:
            exact_frame = filter_option_chain_by_expiry(frame, expiry)
            chains[expiry] = exact_frame if not exact_frame.empty else frame.copy()
            continue
        errors.append(f"{expiry} unavailable: {describe_api_error(ret, frame)}")
    return chains, errors


def split_option_chain_by_expiry(
    chain: pd.DataFrame,
    expiries: list[str],
) -> dict[str, pd.DataFrame]:
    """Split a moomoo option chain range response into exact expiry frames."""

    return {expiry: filter_option_chain_by_expiry(chain, expiry) for expiry in expiries}


def filter_option_chain_by_expiry(chain: pd.DataFrame, expiry: str) -> pd.DataFrame:
    """Filter a chain response to one expiry date when the response has strike_time."""

    if chain.empty:
        return chain
    if "strike_time" not in chain.columns:
        return chain.copy()
    mask = chain["strike_time"].astype(str).str[:10] == expiry
    return chain.loc[mask].copy()


def select_expiries(expirations: pd.DataFrame, limit: int) -> list[str]:
    """Select the nearest option expiries from a moomoo expiration table."""

    if expirations.empty or limit <= 0:
        return []
    frame = expirations.copy()
    if "option_expiry_date_distance" in frame.columns:
        frame = frame.assign(
            _dist=pd.to_numeric(
                frame["option_expiry_date_distance"],
                errors="coerce",
            ),
        )
        frame = frame.sort_values(["_dist", "strike_time"], na_position="last")
    elif "strike_time" in frame.columns:
        frame = frame.sort_values("strike_time")
    else:
        return []
    expiries: list[str] = []
    seen: set[str] = set()
    for value in frame["strike_time"]:
        expiry = str(value)[:10]
        if expiry and expiry not in seen:
            seen.add(expiry)
            expiries.append(expiry)
        if len(expiries) >= limit:
            break
    return expiries


def select_atm_pair(
    chain: pd.DataFrame,
    latest_close: float | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Select an ATM call/put pair from one option chain."""

    if chain.empty or latest_close is None:
        return None
    rows = chain.to_dict("records")
    calls: dict[float, dict[str, Any]] = {}
    puts: dict[float, dict[str, Any]] = {}
    for row in rows:
        strike = safe_float(row.get("strike_price"))
        code = row.get("code")
        option_type = str(row.get("option_type", "")).upper()
        if strike is None or strike <= 0 or not code:
            continue
        if option_type == "CALL":
            calls[strike] = row
        elif option_type == "PUT":
            puts[strike] = row
    common = [strike for strike in calls if strike in puts]
    if not common:
        return None
    # 最近优先；等距时优先取不超过现价的下界 strike（标准 ATM 约定，跨券商可复现）。
    strike = min(
        common,
        key=lambda value: (abs(value - latest_close), value > latest_close),
    )
    return calls[strike], puts[strike]


def option_pair_row(
    ctx: QuoteContext,
    code: str,
    expiry: str,
    pair: tuple[dict[str, Any], dict[str, Any]],
    latest_close: float | None,
    target_date: date,
) -> dict[str, Any]:
    """Build one option analysis row for an ATM call/put pair."""

    call_row, put_row = pair
    call_code = str(call_row.get("code"))
    put_code = str(put_row.get("code"))
    snapshots = option_snapshots(ctx, [call_code, put_code])
    call_snapshot = snapshots.get(call_code, {})
    put_snapshot = snapshots.get(put_code, {})
    call_iv = first_float(call_snapshot, call_row, "option_implied_volatility")
    put_iv = first_float(put_snapshot, put_row, "option_implied_volatility")
    call_oi = first_float(call_snapshot, call_row, "option_open_interest")
    put_oi = first_float(put_snapshot, put_row, "option_open_interest")
    skew = put_iv - call_iv if call_iv is not None and put_iv is not None else None
    pcr = put_oi / call_oi if call_oi and put_oi is not None else None
    risk_score = option_risk_score(call_iv, put_iv, call_oi, put_oi)
    return {
        "underlying": code,
        "target_date": target_date.isoformat(),
        "expiry": expiry,
        "underlying_close": latest_close,
        "strike_price": safe_float(call_row.get("strike_price")),
        "call_code": call_code,
        "put_code": put_code,
        "call_iv": call_iv,
        "put_iv": put_iv,
        "iv_skew": skew,
        "call_oi": call_oi,
        "put_oi": put_oi,
        "pcr": pcr,
        "risk_score": risk_score,
        "risk_label": option_risk_label(risk_score),
        "gap": "",
    }


def option_snapshot(ctx: QuoteContext, option_code: str) -> dict[str, Any]:
    """Fetch one option market snapshot as a dictionary."""

    return option_snapshots(ctx, [option_code]).get(option_code, {})


def option_snapshots(
    ctx: QuoteContext, option_codes: list[str]
) -> dict[str, dict[str, Any]]:
    """Fetch option market snapshots in one paced request keyed by option code."""

    ret, frame = option_quote_call(lambda: ctx.get_market_snapshot(option_codes))
    if ret != ft.RET_OK or not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    snapshots: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict("records"):
        code = str(row.get("code") or "")
        if code:
            snapshots[code] = row
    return snapshots


def first_float(
    preferred: dict[str, Any],
    fallback: dict[str, Any],
    key: str,
) -> float | None:
    """Return the first finite float from snapshot or chain row."""

    return safe_float(preferred.get(key)) or safe_float(fallback.get(key))


def option_risk_score(
    call_iv: float | None,
    put_iv: float | None,
    call_oi: float | None,
    put_oi: float | None,
) -> float | None:
    """Compute the same option risk score family used by strategy features."""

    parts: list[float] = []
    if call_iv is not None and put_iv is not None and call_iv > 0 and put_iv > 0:
        parts.append(features.iv_skew_score(put_iv, call_iv))
    if call_oi is not None and put_oi is not None and call_oi + put_oi > 0:
        parts.append(features.pcr_score(put_oi, call_oi))
    if not parts:
        return None
    return sum(parts) / len(parts)


def option_risk_label(score: float | None) -> str:
    """Map option risk score to a compact report label."""

    if score is None:
        return "N/A"
    if score >= 70:
        return "高风险"
    if score >= 55:
        return "偏谨慎"
    if score <= 35:
        return "偏多"
    return "中性"


def option_gap_row(code: str, target_date: date, gap: str) -> dict[str, Any]:
    """Build an explicit no-data option row."""

    return {
        "underlying": code,
        "target_date": target_date.isoformat(),
        "expiry": "",
        "underlying_close": None,
        "strike_price": None,
        "call_code": "",
        "put_code": "",
        "call_iv": None,
        "put_iv": None,
        "iv_skew": None,
        "call_oi": None,
        "put_oi": None,
        "pcr": None,
        "risk_score": None,
        "risk_label": "N/A",
        "gap": gap,
    }


def write_outputs(
    paths: ReportPaths,
    payload: dict[str, Any],
    stock_rows: list[dict[str, Any]],
    option_rows: list[dict[str, Any]],
) -> None:
    """Write markdown, JSON, and CSV report outputs."""

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    paths.report_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    pd.DataFrame(flatten_stock_rows(stock_rows)).to_csv(
        paths.stock_csv,
        index=False,
        encoding="utf-8-sig",
    )
    pd.DataFrame(option_rows).to_csv(
        paths.option_csv, index=False, encoding="utf-8-sig"
    )
    paths.summary_md.write_text(render_markdown(payload), encoding="utf-8")


def flatten_stock_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten stock rows for CSV output."""

    flat: list[dict[str, Any]] = []
    for row in rows:
        bar = row.get("bar", {})
        core = row.get("core_scores", {})
        diag = row.get("diagnostic_scores", {})
        pos = row.get("position", {})
        flat.append(
            {
                "code": row.get("code"),
                "signal": row.get("signal"),
                "score": row.get("score"),
                "reason": row.get("reason"),
                "close": bar.get("close"),
                "change_rate_pct": bar.get("change_rate_pct"),
                "turnover": bar.get("turnover"),
                "turnover_rate": bar.get("turnover_rate"),
                "atr": row.get("atr"),
                "core_turnover": core.get("turnover"),
                "core_capital": core.get("capital"),
                "core_momentum": core.get("momentum"),
                "rs": diag.get("rs"),
                "short": diag.get("short"),
                "option_iv": diag.get("option_iv"),
                "macro_filter": diag.get("macro_filter"),
                "crypto_filter": diag.get("crypto_filter"),
                "liquidity_ok": row.get("liquidity_ok"),
                "factor_gaps": ",".join(row.get("factor_gaps", [])),
                "risk_warnings": " | ".join(row.get("risk_warnings", [])),
                "buy_block_reasons": " | ".join(row.get("buy_block_reasons", [])),
                "has_position": pos.get("has_position"),
                "qty": pos.get("qty"),
                "cost_price": pos.get("cost_price"),
                "unrealized_return": pos.get("unrealized_return"),
                "drawdown_from_peak": pos.get("drawdown_from_peak"),
                "factor_note": row.get("factor_note"),
            },
        )
    return flat


def render_markdown(payload: dict[str, Any]) -> str:
    """Render the final Feishu Markdown report."""

    summary = payload["summary"]
    stock_rows = payload["stocks"]
    option_rows = payload["options"]
    index_rows = payload["indexes"]
    lines = [
        f"# 美股日报 {summary['target_date']}",
        "",
        f"- 生成时间: {summary['generated_at']}",
        f"- 观察列表: {summary['watchlist_count']} 只；BUY {summary['signals']['BUY']} / HOLD {summary['signals']['HOLD']} / SELL {summary['signals']['SELL']}",
        f"- 主策略阈值: BUY < {summary['thresholds']['buy_threshold']:.1f}；SELL >= {summary['thresholds']['sell_threshold']:.1f}",
        "- 数据口径: OpenD 日线、快照、资金分布、期权链；期权为限深 ATM 摘要。",
        "",
        "## 30秒结论",
        "",
        *quick_takeaways(stock_rows, option_rows),
        "",
        "## 指数行情",
        "",
        "| 指数 | 来源 | 收盘 | 涨跌幅 | 成交额/量 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in index_rows:
        lines.append(
            "| {label} | {source} | {close} | {change} | {turnover} |".format(
                label=row["label"],
                source=f"{row['source_code']} {row['source_kind']}",
                close=format_number(row.get("close")),
                change=format_pct_value(row.get("change_rate_pct")),
                turnover=format_money(row.get("turnover") or row.get("volume")),
            ),
        )

    lines.extend(
        [
            "",
            "## 观察列表主策略信号",
            "",
            "| 代码 | 信号 | 分数 | 收盘 | 涨跌幅 | 流动性 | 因子摘要 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ],
    )
    for row in stock_rows:
        bar = row.get("bar", {})
        lines.append(
            "| {code} | {signal} | {score} | {close} | {change} | {liq} | {note} |".format(
                code=row["code"],
                signal=row["signal"],
                score=format_number(row.get("score"), 1),
                close=format_number(bar.get("close")),
                change=format_pct_value(bar.get("change_rate_pct")),
                liq="OK" if row.get("liquidity_ok") else "LOW",
                note=str(row.get("factor_note", "")).replace("|", "/"),
            ),
        )

    lines.extend(
        [
            "",
            "## 个股深度观察",
            "",
        ],
    )
    for row in stock_rows:
        bar = row.get("bar", {})
        core = row.get("core_scores", {})
        gaps = ", ".join(row.get("factor_gaps", [])) or "无"
        lines.append(
            "- {code}: {signal} score={score}; close={close}, change={change}, "
            "turnover={turnover}; core(turnover/capital/momentum)="
            "{turnover_score}/{capital_score}/{momentum_score}; gaps={gaps}; reason={reason}".format(
                code=row["code"],
                signal=row["signal"],
                score=format_number(row.get("score"), 1),
                close=format_number(bar.get("close")),
                change=format_pct_value(bar.get("change_rate_pct")),
                turnover=format_money(bar.get("turnover")),
                turnover_score=format_number(core.get("turnover"), 1),
                capital_score=format_number(core.get("capital"), 1),
                momentum_score=format_number(core.get("momentum"), 1),
                gaps=gaps,
                reason=str(row.get("reason", "")).replace("\n", " "),
            ),
        )

    lines.extend(
        [
            "",
            "## 期权限深分析",
            "",
            "| 标的 | 到期日 | 行权价 | IV skew | PCR | 风险 | 缺口 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ],
    )
    for row in option_rows:
        lines.append(
            "| {code} | {expiry} | {strike} | {skew} | {pcr} | {risk} | {gap} |".format(
                code=row["underlying"],
                expiry=row.get("expiry") or "-",
                strike=format_number(row.get("strike_price")),
                skew=format_number(row.get("iv_skew"), 2),
                pcr=format_number(row.get("pcr"), 2),
                risk=row.get("risk_label") or "N/A",
                gap=row.get("gap") or "",
            ),
        )

    if summary["warnings"]:
        lines.extend(["", "## 数据缺口与风险提示", ""])
        lines.extend(f"- {warning}" for warning in summary["warnings"])
    lines.extend(
        [
            "",
            "## 输出文件",
            "",
            f"- JSON: {summary['paths']['report_json']}",
            f"- 股票因子 CSV: {summary['paths']['stock_csv']}",
            f"- 期权 CSV: {summary['paths']['option_csv']}",
            "",
            "数据来源于 moomoo OpenD。本报告仅作研究观察，不构成实盘交易指令。",
        ],
    )
    return "\n".join(lines) + "\n"


def quick_takeaways(
    stock_rows: list[dict[str, Any]],
    option_rows: list[dict[str, Any]],
) -> list[str]:
    """Build concise top-level conclusions."""

    buy = [row for row in stock_rows if row["signal"] == Signal.BUY.value]
    sell = [row for row in stock_rows if row["signal"] == Signal.SELL.value]
    high_option = [
        row
        for row in option_rows
        if row.get("risk_score") is not None and row["risk_score"] >= 70
    ]
    lines = [
        "- 主策略低风险候选: "
        + (", ".join(row["code"] for row in buy[:8]) if buy else "无"),
        "- 主策略卖出/高风险: "
        + (", ".join(row["code"] for row in sell[:8]) if sell else "无"),
        "- 期权高风险提示: "
        + (
            ", ".join(
                f"{row['underlying']}({row['risk_score']:.1f})"
                for row in high_option[:8]
            )
            if high_option
            else "无明显高风险 ATM skew/PCR"
        ),
    ]
    return lines


def format_number(value: Any, digits: int = 2) -> str:
    """Format a numeric value for Markdown."""

    numeric = safe_float(value)
    if numeric is None:
        return "-"
    return f"{numeric:.{digits}f}"


def format_pct_value(value: Any) -> str:
    """Format moomoo percentage values, which are already percent units."""

    numeric = safe_float(value)
    if numeric is None:
        return "-"
    return f"{numeric:.2f}%"


def format_money(value: Any) -> str:
    """Format large turnover or volume values compactly."""

    numeric = safe_float(value)
    if numeric is None:
        return "-"
    if abs(numeric) >= 1_000_000_000:
        return f"{numeric / 1_000_000_000:.2f}B"
    if abs(numeric) >= 1_000_000:
        return f"{numeric / 1_000_000:.2f}M"
    return f"{numeric:.0f}"


def json_default(value: Any) -> Any:
    """JSON serializer fallback for paths and datetime values."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def run_lark_command(command: list[str], output_path: Path) -> dict[str, Any]:
    """Run one lark-cli command and persist the JSON receipt."""

    resolved_command = resolve_lark_command(command)
    completed = subprocess.run(
        resolved_command,
        check=False,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    receipt = {
        "command": resolved_command,
        "returncode": completed.returncode,
        "stdout": parse_json_or_text(completed.stdout),
        "stderr": completed.stderr,
    }
    output_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise ReportError(f"lark command failed: {' '.join(command)}")
    return receipt


def resolve_lark_command(command: list[str]) -> list[str]:
    """Resolve lark-cli for Windows subprocess execution."""

    if not command or command[0] != "lark-cli":
        return command
    cmd_executable = shutil.which("lark-cli.cmd")
    if cmd_executable is not None:
        ps1 = Path(cmd_executable).with_suffix(".ps1")
        if ps1.exists():
            return [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ps1),
                *command[1:],
            ]
        return [cmd_executable, *command[1:]]

    executable = shutil.which("lark-cli") or shutil.which("lark-cli.ps1")
    if executable is None:
        return command
    if executable.lower().endswith(".ps1"):
        return [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            executable,
            *command[1:],
        ]
    return [executable, *command[1:]]


def parse_json_or_text(raw: str) -> Any:
    """Parse CLI stdout as JSON when possible."""

    text = raw.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def find_first_key(payload: Any, keys: set[str]) -> str | None:
    """Recursively find the first non-empty string value for any key."""

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in keys and isinstance(value, str) and value:
                return value
            found = find_first_key(value, keys)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = find_first_key(item, keys)
            if found:
                return found
    return None


def send_to_lark(
    paths: ReportPaths,
    target_date: date,
    chat_id: str,
    dry_run: bool,
    force_send: bool,
) -> LarkResult:
    """Create a Feishu Markdown file, send a message, and verify readback."""

    if paths.lark_send_json.exists() and not force_send:
        existing = json.loads(paths.lark_send_json.read_text(encoding="utf-8"))
        message_id = find_first_key(existing, {"message_id", "messageId"})
        if message_id:
            return LarkResult(
                doc_url=find_first_key(existing, {"doc_url", "url", "file_url"}),
                file_token=None,
                message_id=message_id,
                skipped=True,
            )

    title = f"美股日报_{ymd(target_date)}.md"
    if dry_run:
        dry_receipts = {
            paths.lark_create_json: [
                "lark-cli",
                "markdown",
                "+create",
                "--file",
                str(paths.summary_md),
                "--name",
                title,
                "--dry-run",
            ],
            paths.lark_send_json: [
                "lark-cli",
                "api",
                "POST",
                "/open-apis/im/v1/messages",
                "--params",
                f"@{paths.lark_send_params_json}",
                "--data",
                f"@{paths.lark_send_body_json}",
                "--dry-run",
            ],
        }
        write_lark_card_files(
            paths,
            target_date,
            chat_id,
            doc_url="https://www.feishu.cn/file/dry-run",
        )
        for path, command in dry_receipts.items():
            path.write_text(
                json.dumps(
                    {"dry_run": True, "command": command}, ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
        paths.lark_fetch_json.write_text(
            json.dumps(
                {"dry_run": True, "skipped": "no file token"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        paths.lark_message_json.write_text(
            json.dumps(
                {"dry_run": True, "skipped": "no message id"},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return LarkResult(None, None, None, skipped=False)

    create = run_lark_command(
        [
            "lark-cli",
            "markdown",
            "+create",
            "--file",
            str(paths.summary_md),
            "--name",
            title,
        ],
        paths.lark_create_json,
    )
    file_token = find_first_key(create, {"file_token", "token", "obj_token"})
    doc_url = find_first_key(create, {"doc_url", "url", "file_url", "web_url"})
    if not file_token:
        raise ReportError("lark markdown create did not return file_token")

    write_lark_card_files(paths, target_date, chat_id, doc_url)
    send = run_lark_command(
        [
            "lark-cli",
            "api",
            "POST",
            "/open-apis/im/v1/messages",
            "--params",
            f"@{paths.lark_send_params_json}",
            "--data",
            f"@{paths.lark_send_body_json}",
        ],
        paths.lark_send_json,
    )
    message_id = find_first_key(send, {"message_id", "messageId"})
    if not message_id:
        raise ReportError("lark message send did not return message_id")

    run_lark_command(
        ["lark-cli", "markdown", "+fetch", "--file-token", file_token],
        paths.lark_fetch_json,
    )
    message = run_lark_command(
        ["lark-cli", "api", "GET", f"/open-apis/im/v1/messages/{message_id}"],
        paths.lark_message_json,
    )
    verify_lark_card_readback(message, doc_url, target_date)
    return LarkResult(doc_url, file_token, message_id, skipped=False)


def build_lark_message_text(
    target_date: date,
    summary_path: Path,
    doc_url: str | None,
) -> str:
    """Build a plain Feishu group message body."""

    content = summary_path.read_text(encoding="utf-8")
    takeaways = []
    in_takeaway = False
    for line in content.splitlines():
        if line.startswith("## 30秒结论"):
            in_takeaway = True
            continue
        if in_takeaway and line.startswith("## "):
            break
        if in_takeaway and line.strip():
            takeaways.append(line)
    parts = [f"美股日报 {target_date.isoformat()}"]
    parts.extend(line.lstrip("- ").strip() for line in takeaways[:5])
    if doc_url:
        parts.append(f"完整报告: {doc_url}")
    return "；".join(part for part in parts if part)


def write_lark_card_files(
    paths: ReportPaths,
    target_date: date,
    chat_id: str,
    doc_url: str | None,
) -> None:
    """Write Feishu interactive card and API request JSON files."""

    card = build_lark_summary_card(target_date, paths.summary_md, doc_url)
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    params = {"receive_id_type": "chat_id"}
    paths.lark_card_json.write_text(
        json.dumps(card, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths.lark_send_body_json.write_text(
        json.dumps(body, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths.lark_send_params_json.write_text(
        json.dumps(params, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def build_lark_summary_card(
    target_date: date,
    summary_path: Path,
    doc_url: str | None,
) -> dict[str, Any]:
    """Build a Feishu interactive card with summary and full document link."""

    takeaways = extract_takeaways(summary_path)
    summary_content = "\n".join(takeaways[:4]) or "- 暂无摘要"
    doc_link_text = "完整个股深度、全因子策略信号和期权限深分析请打开云文档查看。"
    if doc_url:
        doc_link_text += f"\n\n[打开完整云文档]({doc_url})"
    elements: list[dict[str, Any]] = [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**30秒结论**\n{summary_content}",
            },
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": doc_link_text,
            },
        },
    ]
    if doc_url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {
                            "tag": "plain_text",
                            "content": "打开完整云文档",
                        },
                        "type": "primary",
                        "url": doc_url,
                    },
                ],
            },
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"美股日报 {target_date.isoformat()}",
            },
        },
        "elements": elements,
    }


def extract_takeaways(summary_path: Path) -> list[str]:
    """Extract the top takeaway bullet list from summary markdown."""

    content = summary_path.read_text(encoding="utf-8")
    takeaways: list[str] = []
    in_takeaway = False
    for line in content.splitlines():
        if line.startswith("## 30秒结论"):
            in_takeaway = True
            continue
        if in_takeaway and line.startswith("## "):
            break
        if in_takeaway and line.strip():
            takeaways.append(line.strip())
    return takeaways


def has_interactive_msg_type(payload: Any) -> bool:
    """Recursively check the readback carries a real msg_type == 'interactive'.

    Structured match on the actual ``msg_type`` field, so an incidental
    "interactive" substring in unrelated text cannot satisfy fail-closed.
    """

    if isinstance(payload, dict):
        for key, value in payload.items():
            if key == "msg_type" and value == "interactive":
                return True
            if has_interactive_msg_type(value):
                return True
    elif isinstance(payload, list):
        return any(has_interactive_msg_type(item) for item in payload)
    return False


def verify_lark_card_readback(
    receipt: dict[str, Any],
    doc_url: str | None,
    target_date: date,
) -> None:
    """Verify raw message readback contains the interactive card and doc link."""

    stdout = receipt.get("stdout")
    if not has_interactive_msg_type(stdout):
        raise ReportError("lark card readback missing interactive msg_type")
    raw = json.dumps(stdout, ensure_ascii=False)
    if f"美股日报 {target_date.isoformat()}" not in raw:
        raise ReportError("lark card readback missing report title")
    if doc_url and doc_url not in raw:
        raise ReportError("lark card readback missing full report link")


def text_message_content(text: str) -> str:
    """Build Feishu text message content JSON."""

    return json.dumps({"text": text}, ensure_ascii=False)


def build_payload(
    target_date: date,
    generated_at: datetime,
    paths: ReportPaths,
    config: StrategyConfig,
    codes: tuple[str, ...],
    indexes: list[dict[str, Any]],
    stocks: list[dict[str, Any]],
    options: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    """Build the final structured report payload."""

    signals = {
        name: sum(1 for row in stocks if row["signal"] == name)
        for name in (Signal.BUY.value, Signal.HOLD.value, Signal.SELL.value)
    }
    warnings = list(warnings)
    # 期权为可选限深口径：若有标的但所有期权行均为缺口，显式告警（不 fail-closed）。
    if stocks and options and all(row.get("gap") for row in options):
        warnings.append("期权数据整体缺失：所有标的均无有效 ATM 期权对")
    return {
        "summary": {
            "target_date": target_date.isoformat(),
            "generated_at": generated_at.isoformat(),
            "watchlist_count": len(codes),
            "signals": signals,
            "thresholds": {
                "buy_threshold": config.buy_threshold,
                "sell_threshold": config.sell_threshold,
                "stop_loss_pct": config.stop_loss_pct,
                "trailing_stop_pct": config.trailing_stop_pct,
                "min_daily_turnover_usd": config.min_daily_turnover_usd,
            },
            "active_weights": config.active_weights(),
            "warnings": warnings,
            "paths": {
                "summary_md": str(paths.summary_md),
                "report_json": str(paths.report_json),
                "stock_csv": str(paths.stock_csv),
                "option_csv": str(paths.option_csv),
            },
        },
        "indexes": indexes,
        "stocks": stocks,
        "options": options,
    }


def validate_report_payload(payload: dict[str, Any]) -> None:
    """Fail closed on missing critical report content."""

    if not payload["indexes"]:
        raise ReportError("no index or proxy bars available")
    stocks = payload["stocks"]
    if not stocks:
        raise ReportError("watchlist analysis is empty")
    missing_bars = [
        row["code"]
        for row in stocks
        if not row.get("bar") or row["bar"].get("close") is None
    ]
    if missing_bars:
        raise ReportError("missing daily bars for watchlist: " + ",".join(missing_bars))


def write_failure(paths: ReportPaths, target_date: date, message: str) -> None:
    """Persist a fail-closed report artifact."""

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_date": target_date.isoformat(),
        "failed_at": datetime.now(BEIJING_TZ).isoformat(),
        "error": message,
    }
    paths.failure_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths.summary_md.write_text(
        f"# 美股日报失败 {target_date.isoformat()}\n\n- 失败原因: {message}\n",
        encoding="utf-8",
    )


def run_report(
    args: argparse.Namespace,
    quote_ctx: QuoteContext | None = None,
) -> tuple[dict[str, Any], LarkResult | None, ReportPaths]:
    """Run the full read-only daily report workflow."""

    explicit_date = bool(args.date)
    now_us = datetime.now(US_TZ)
    target_date = infer_target_date(
        now=now_us,
        explicit_date=date.fromisoformat(args.date) if args.date else None,
    )
    paths = build_paths(target_date, Path(args.output_dir) if args.output_dir else None)
    try:
        validate_target_date(target_date, now_us, explicit_date, args.force)
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        codes = load_us_watchlist(Path(args.watchlist))
        if not codes:
            raise ReportError("US watchlist is empty")
        config = StrategyConfig.from_env()
        if quote_ctx is None:
            quote_ctx = ft.OpenQuoteContext(host=args.host, port=args.port)
            close_quote = True
        else:
            close_quote = False
        try:
            data = DataAccess(quote_ctx, ReadOnlyTradeContext(), config)
            with anchor_strategy_date(target_date):
                indexes, index_errors = fetch_index_bars(data, target_date)
                stocks, stock_errors = analyze_stocks(data, config, codes, target_date)
                options, option_errors = analyze_options(
                    quote_ctx, codes, stocks, target_date
                )
            warnings = index_errors + stock_errors + option_errors
            payload = build_payload(
                target_date=target_date,
                generated_at=datetime.now(BEIJING_TZ),
                paths=paths,
                config=config,
                codes=codes,
                indexes=indexes,
                stocks=stocks,
                options=options,
                warnings=warnings,
            )
            validate_report_payload(payload)
            write_outputs(paths, payload, stocks, options)
            lark_result = None
            if args.send:
                lark_result = send_to_lark(
                    paths,
                    target_date,
                    args.chat_id,
                    args.dry_run_lark,
                    args.force_send,
                )
            return payload, lark_result, paths
        finally:
            if close_quote and hasattr(quote_ctx, "close"):
                quote_ctx.close()
    except Exception as exc:
        write_failure(paths, target_date, str(exc))
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the US daily report."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default="", help="Explicit US trade date, YYYY-MM-DD."
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--chat-id", default=DEFAULT_CHAT_ID)
    parser.add_argument("--send", dest="send", action="store_true")
    parser.add_argument("--no-send", dest="send", action="store_false")
    parser.add_argument("--dry-run-lark", action="store_true")
    parser.add_argument(
        "--force", action="store_true", help="Bypass after-close readiness check."
    )
    parser.add_argument(
        "--force-send",
        action="store_true",
        help="Send even if lark_send.json already has a message_id.",
    )
    parser.set_defaults(send=False)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""

    args = parse_args(argv)
    try:
        payload, lark_result, paths = run_report(args)
    except Exception as exc:  # noqa: BLE001 - command line must preserve fail evidence.
        print(f"FAIL_CLOSED: {exc}", file=sys.stderr)
        return 1
    result = {
        "target_date": payload["summary"]["target_date"],
        "summary_md": str(paths.summary_md.resolve()),
        "report_json": str(paths.report_json.resolve()),
        "stock_csv": str(paths.stock_csv.resolve()),
        "option_csv": str(paths.option_csv.resolve()),
        "doc_url": lark_result.doc_url if lark_result else None,
        "message_id": lark_result.message_id if lark_result else None,
        "lark_skipped": lark_result.skipped if lark_result else False,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
