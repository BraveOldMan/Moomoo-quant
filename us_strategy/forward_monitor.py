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
  MONITOR_IGNORE_HOURS "true" to log outside RTH too (default false)
  plus all StrategyConfig.from_env vars (OPEND_HOST, WATCHLIST, ...)

Run:  python -m us_strategy.forward_monitor
Stop: Ctrl+C
"""

import dataclasses
import logging
import os
import time

import moomoo as ft

from .config import StrategyConfig, _load_watchlist
from .data_access import DataAccess
from .main import _is_market_open
from .persistence import SignalLogStore
from .signals import SignalCalculator

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("forward_monitor")


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

    cfg = _enable_all_factors(StrategyConfig.from_env())
    codes = list(_load_watchlist())
    if not codes:
        logger.error("watchlist is empty (no WATCHLIST env and no watchlist.txt)")
        return
    logger.info(
        "monitoring %d symbols, interval=%.0fs, db=%s",
        len(codes),
        interval,
        cfg.db_path,
    )

    quote = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    try:
        quote.subscribe(
            codes,
            [ft.SubType.QUOTE, ft.SubType.TICKER, ft.SubType.ORDER_BOOK],
            subscribe_push=False,
        )
    except Exception as exc:
        logger.warning("subscribe failed (microstructure may be unavailable): %s", exc)

    store = SignalLogStore(cfg.db_path)
    data = DataAccess(quote, None, cfg)
    calc = SignalCalculator(data, cfg, signal_log=store)

    rounds = 0
    try:
        while True:
            if not ignore_hours and not _is_market_open(cfg):
                logger.info(
                    "market closed; skip round (set MONITOR_IGNORE_HOURS=true to override)"
                )
            else:
                logged = 0
                for code in codes:
                    try:
                        res = calc.calculate(
                            code
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


if __name__ == "__main__":
    run()
