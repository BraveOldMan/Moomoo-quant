# -*- coding: utf-8 -*-
"""Forward-logging monitor: score the watchlist on a loop and persist every
factor score into signal_log, WITHOUT placing any orders.

Purpose: collect (factor scores @T, price @T) so that, once T+N realized
returns exist, analysis.forward_ic_from_log can calibrate the un-validated
microstructure / short / option factors before they are ever weighted.

All extension factors are force-enabled here so their scores get logged.
This process never trades; it only reads quotes and writes signal_log.

Env vars:
  MONITOR_INTERVAL_S   seconds between rounds (default 300)
  MONITOR_MAX_ROUNDS   stop after N rounds (default 0 = run forever)
  MONITOR_MARKET_SESSIONS comma-separated PRE/RTH/AFTER sessions to log
  MONITOR_IGNORE_HOURS "true" to log outside RTH too (default false)
  plus all StrategyConfig.from_env vars (OPEND_HOST, WATCHLIST, ...)

Run:  python -m us_strategy.forward_monitor
Stop: Ctrl+C
"""

import dataclasses
import logging
import os
import time
from datetime import datetime, time as _time

import moomoo as ft

from .config import StrategyConfig, _load_watchlist
from .data_access import DataAccess
from .clock import market_datetime
from .market_calendar import is_trading_day
from .persistence import SignalLogStore
from .signals import SignalCalculator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("forward_monitor")

_PRE_OPEN = _time(4, 0)
_RTH_OPEN = _time(9, 30)
_RTH_CLOSE = _time(16, 0)
_AFTER_CLOSE = _time(20, 0)
_VALID_SESSIONS = {"PRE", "RTH", "AFTER"}


def _enable_all_factors(cfg: StrategyConfig) -> StrategyConfig:
    return dataclasses.replace(
        cfg,
        use_rs=True,
        use_orb=True,
        use_vwap_signal=True,
        use_order_flow=True,
        use_dark_pool_proxy=True,
        use_order_book_imbalance=True,
        use_order_book_pressure=True,
        use_order_book_metrics=True,
        use_l2_imbalance_tracker=True,
        use_intraday_flow=True,
        use_short_metrics=True,
        use_option_iv=True,
        use_macro_filter=True,
        use_crypto_filter=True,
    )


def run() -> None:
    interval = float(os.environ.get("MONITOR_INTERVAL_S", "300"))
    max_rounds = int(os.environ.get("MONITOR_MAX_ROUNDS", "0"))
    ignore_hours = os.environ.get("MONITOR_IGNORE_HOURS", "false").lower() == "true"
    target_sessions = _parse_market_sessions(
        os.environ.get("MONITOR_MARKET_SESSIONS", "")
    )

    cfg = _enable_all_factors(StrategyConfig.from_env())
    codes = list(_load_watchlist())
    if not codes:
        logger.error("watchlist is empty (no WATCHLIST env and no watchlist.txt)")
        return
    logger.info(
        "monitoring %d symbols, interval=%.0fs, db=%s sessions=%s",
        len(codes),
        interval,
        cfg.db_path,
        ",".join(sorted(target_sessions)) if target_sessions else "RTH",
    )

    quote = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    try:
        _subscribe_forward_quotes(quote, codes, target_sessions)
    except Exception as exc:
        logger.warning("subscribe failed (microstructure may be unavailable): %s", exc)

    store = SignalLogStore(cfg.db_path)
    data = DataAccess(quote, None, cfg)
    calc = SignalCalculator(data, cfg, signal_log=store)

    rounds = 0
    try:
        while True:
            market_session = _market_session(cfg)
            should_log = _should_log_session(
                market_session,
                target_sessions,
                ignore_hours,
            )
            if not should_log:
                logger.info(
                    "market_session=%s skipped (MONITOR_MARKET_SESSIONS=%s)",
                    market_session,
                    ",".join(sorted(target_sessions)) if target_sessions else "RTH",
                )
            else:
                logged = 0
                for code in codes:
                    try:
                        last_price = _session_price(data, code, market_session)
                        if last_price is None:
                            logger.warning(
                                "%s missing %s price; skip signal log",
                                code,
                                market_session,
                            )
                            continue
                        res = calc.calculate(
                            code,
                            last_price=last_price,
                            market_session=market_session,
                        )  # logs into signal_log when price present
                    except Exception as exc:
                        logger.warning("%s calculate failed: %s", code, exc)
                        continue
                    if res is not None:
                        logged += 1
                        logger.info(
                            "%s comp=%.1f scores=%s",
                            code,
                            res.composite_score,
                            res.scores,
                        )
                logger.info(
                    "round %d done: logged %d/%d", rounds + 1, logged, len(codes)
                )
            rounds += 1
            if max_rounds and rounds >= max_rounds:
                logger.info("reached MONITOR_MAX_ROUNDS=%d, exit", max_rounds)
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        logger.info("interrupted, shutting down")
    finally:
        try:
            quote.unsubscribe_all()
        except Exception:
            pass
        quote.close()


def _parse_market_sessions(raw: str) -> set[str]:
    """Parse MONITOR_MARKET_SESSIONS into PRE/RTH/AFTER tokens."""

    sessions = {
        item.strip().upper()
        for item in raw.split(",")
        if item.strip()
    }
    unknown = sessions - _VALID_SESSIONS
    if unknown:
        logger.warning("ignore unknown MONITOR_MARKET_SESSIONS values: %s", unknown)
    return sessions & _VALID_SESSIONS


def _market_session(
    cfg: StrategyConfig,
    now: datetime | None = None,
) -> str:
    """Return PRE/RTH/AFTER/CLOSED using New York market time."""

    current = market_datetime(cfg.market_timezone, now=now)
    if not is_trading_day(current.date()):
        return "CLOSED"
    t = current.time().replace(second=0, microsecond=0)
    if _PRE_OPEN <= t < _RTH_OPEN:
        return "PRE"
    if _RTH_OPEN <= t < _RTH_CLOSE:
        return "RTH"
    if _RTH_CLOSE <= t < _AFTER_CLOSE:
        return "AFTER"
    return "CLOSED"


def _should_log_session(
    market_session: str,
    target_sessions: set[str],
    ignore_hours: bool,
) -> bool:
    """Decide whether the current forward-monitor round should be logged."""

    if target_sessions:
        return market_session in target_sessions
    if ignore_hours:
        return market_session != "CLOSED"
    return market_session == "RTH"


def _subscribe_forward_quotes(
    quote,
    codes: list[str],
    target_sessions: set[str],
) -> None:
    """Subscribe forward monitor quotes with extended-hours support when needed."""

    kwargs = {
        "subscribe_push": False,
    }
    if target_sessions & {"PRE", "AFTER"}:
        kwargs["extended_time"] = True
        kwargs["session"] = ft.Session.ETH
    quote.subscribe(
        codes,
        [ft.SubType.QUOTE, ft.SubType.TICKER, ft.SubType.ORDER_BOOK],
        **kwargs,
    )


def _session_price(data: DataAccess, code: str, market_session: str) -> float | None:
    """Read the best available price for the current market session."""

    ret, snap = data.get_market_snapshot(code)
    if ret != ft.RET_OK or snap.empty:
        return None
    row = snap.iloc[0]
    if market_session == "PRE":
        return _first_positive(row, ("pre_price",)) or _ticker_price(data, code)
    if market_session == "AFTER":
        return _first_positive(row, ("after_price",)) or _ticker_price(data, code)
    return _first_positive(row, ("last_price",))


def _first_positive(row, fields: tuple[str, ...]) -> float | None:
    for field in fields:
        try:
            value = float(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _ticker_price(data: DataAccess, code: str) -> float | None:
    """Read the latest subscribed ticker price for extended-hours fallback."""

    ret, ticker = data.get_rt_ticker(code, 1)
    if ret != ft.RET_OK or ticker.empty:
        return None
    return _first_positive(ticker.iloc[0], ("price",))


if __name__ == "__main__":
    run()
