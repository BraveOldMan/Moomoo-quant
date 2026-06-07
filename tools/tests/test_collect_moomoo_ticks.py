from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from dark_pool_proxy import DarkPoolProxyConfig
from order_book_l2 import (
    L2ImbalanceConfig,
    L2ImbalanceTracker,
    build_order_book_records,
    compute_order_book_metrics,
    evaluate_l2_imbalance,
)
from tools.collect_moomoo_ticks import (
    TickStore,
    TickWriter,
    frame_to_records,
    load_watchlist,
)


def test_load_watchlist_filters_prefix_and_dedups(tmp_path) -> None:
    watchlist = tmp_path / "watchlist.txt"
    watchlist.write_text(
        "HK.00700 # Tencent\nUS.AAPL\nHK.00700\nHK.00981, HK.02899\n",
        encoding="utf-8",
    )

    assert load_watchlist(watchlist, "HK.") == (
        "HK.00700",
        "HK.00981",
        "HK.02899",
    )


def test_frame_to_records_normalizes_tick_fields() -> None:
    frame = pd.DataFrame(
        [
            {
                "code": "US.AAPL",
                "name": "Apple",
                "time": "2026-06-05 09:30:01.123",
                "price": 200.5,
                "volume": 100,
                "turnover": 20050.0,
                "ticker_direction": "BUY",
                "sequence": 123456789,
                "type": "AUTO_MATCH",
            }
        ]
    )

    records = frame_to_records(frame, "cache", "run-1")

    assert len(records) == 1
    assert records[0]["_code"] == "US.AAPL"
    assert records[0]["market"] == "US"
    assert records[0]["sequence"] == "123456789"
    assert records[0]["ts_utc"].startswith("2026-06-05T13:30:01.123")


def test_tick_store_upserts_duplicate_sequence(tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    store = TickStore(db_path, "run-1")
    store.start_run(("US",), ("US.AAPL",))
    records = frame_to_records(
        pd.DataFrame(
            [
                {
                    "code": "US.AAPL",
                    "name": "Apple",
                    "time": "2026-06-05 09:30:01",
                    "price": 200.0,
                    "volume": 100,
                    "turnover": 20000.0,
                    "ticker_direction": "BUY",
                    "sequence": 1,
                    "type": "AUTO_MATCH",
                },
                {
                    "code": "US.AAPL",
                    "name": "Apple",
                    "time": "2026-06-05 09:30:01",
                    "price": 200.0,
                    "volume": 100,
                    "turnover": 20000.0,
                    "ticker_direction": "BUY",
                    "sequence": 1,
                    "type": "AUTO_MATCH",
                },
            ]
        ),
        "cache",
        "run-1",
    )
    store.insert_records(records)
    store.finish_run("success")
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM realtime_ticks").fetchone()[0] == 1
        assert conn.execute("SELECT rows_written FROM tick_runs").fetchone()[0] == 2
    finally:
        conn.close()


def test_tick_store_persists_low_frequency_quote_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    store = TickStore(db_path, "run-1")
    store.start_run(("HK",), ("HK.00700",))
    frame = pd.DataFrame(
        [
            {
                "code": "HK.00700",
                "name": "Tencent",
                "last_price": 400.0,
                "cur_price": 400.0,
                "bid_price": 399.8,
                "ask_price": 400.2,
                "bid_vol": 1000,
                "ask_vol": 1200,
                "volume": 500_000,
                "turnover": 200_000_000.0,
                "turnover_rate": 0.5,
                "market_status": "OPEN",
                "update_time": "2026-06-05 10:00:00",
                "dark_status": "TRADING",
                "sec_status": "NORMAL",
                "unexpected_field": "kept in payload",
            }
        ]
    )

    assert store.insert_quote_snapshot_records(frame, "poll") == 1
    store.finish_run("success")
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT _code,
                   market,
                   last_price,
                   bid_price,
                   ask_price,
                   turnover,
                   market_status,
                   dark_status,
                   sec_status,
                   _payload_json
              FROM realtime_quote_snapshots
            """
        ).fetchone()
        assert row[:9] == (
            "HK.00700",
            "HK",
            400.0,
            399.8,
            400.2,
            200_000_000.0,
            "OPEN",
            "TRADING",
            "NORMAL",
        )
        assert "unexpected_field" in row[9]
        assert conn.execute(
            "SELECT quote_snapshots FROM tick_runs"
        ).fetchone() == (1,)
    finally:
        conn.close()


def test_tick_store_persists_order_book_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    store = TickStore(db_path, "run-1")
    store.start_run(("US",), ("US.AAPL",))
    book = {
        "code": "US.AAPL",
        "name": "Apple",
        "Bid": [(10.0, 1000, 2, {}), (9.99, 500, 1, {})],
        "Ask": [(10.01, 800, 2, {}), (10.02, 400, 1, {})],
    }
    snapshot, levels, metrics = build_order_book_records(
        book,
        run_id="run-1",
        source="cache",
        snapshot_id="snapshot-1",
    )

    store.insert_order_book_records(snapshot, levels, metrics)
    store.finish_run("success")
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM order_book_snapshots").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM order_book_levels").fetchone()[0] == 4
        assert conn.execute("SELECT COUNT(*) FROM order_book_metrics").fetchone()[0] == 1
        assert conn.execute(
            "SELECT order_book_snapshots, order_book_levels FROM tick_runs"
        ).fetchone() == (1, 4)
    finally:
        conn.close()


def test_tick_store_persists_broker_queue_snapshot(tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    store = TickStore(db_path, "run-1")
    store.start_run(("HK",), ("HK.00700",))
    bid = pd.DataFrame(
        [
            {
                "bid_broker_id": "B1",
                "bid_broker_name": "Broker 1",
                "bid_broker_pos": 1,
                "order_id": "b1",
                "order_volume": 1000,
            }
        ]
    )
    ask = pd.DataFrame(
        [
            {
                "ask_broker_id": "A1",
                "ask_broker_name": "Broker A",
                "ask_broker_pos": 1,
                "order_id": "a1",
                "order_volume": 3000,
            },
            {
                "ask_broker_id": "A2",
                "ask_broker_name": "Broker B",
                "ask_broker_pos": 2,
                "order_id": "a2",
                "order_volume": 1000,
            },
        ]
    )

    store.insert_broker_queue_records("HK.00700", bid, ask, "poll")
    store.rebuild_microstructure_daily_features()
    store.finish_run("success")
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM broker_queue_snapshots").fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM broker_queue_levels").fetchone()[0]
            == 3
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM broker_queue_metrics").fetchone()[0]
            == 1
        )

        metric = conn.execute(
            """
            SELECT bid_count,
                   ask_count,
                   bid_order_volume,
                   ask_order_volume,
                   ask_ratio,
                   ask_volume_share,
                   score,
                   ask_top_broker_id,
                   ask_top_broker_volume,
                   ask_top_broker_share
              FROM broker_queue_metrics
             WHERE _code = 'HK.00700'
            """
        ).fetchone()
        assert metric[:4] == (1, 2, 1000.0, 4000.0)
        assert metric[4] == pytest.approx(2 / 3)
        assert metric[5] == pytest.approx(0.8)
        assert metric[6] == pytest.approx(200 / 3)
        assert metric[7:] == ("A1", 3000.0, 0.75)

        daily = conn.execute(
            """
            SELECT broker_snapshot_count,
                   broker_score_avg,
                   broker_score_max,
                   broker_ask_ratio_avg,
                   broker_ask_volume_share_avg
              FROM microstructure_daily_features
             WHERE _code = 'HK.00700'
            """
        ).fetchone()
        assert daily[0] == 1
        assert daily[1] == pytest.approx(200 / 3)
        assert daily[2] == pytest.approx(200 / 3)
        assert daily[3] == pytest.approx(2 / 3)
        assert daily[4] == pytest.approx(0.8)

        assert conn.execute(
            """
            SELECT broker_queue_snapshots,
                   broker_queue_levels,
                   broker_queue_metric_rows,
                   microstructure_daily_feature_rows
              FROM tick_runs
            """
        ).fetchone() == (1, 3, 1, 1)
    finally:
        conn.close()


def test_tick_writer_persists_derived_microstructure_rows(tmp_path) -> None:
    db_path = tmp_path / "ticks.db"
    store = TickStore(db_path, "run-1")
    store.start_run(("US",), ("US.AAPL",))
    writer = TickWriter(
        store,
        batch_size=1,
        flush_interval=0.01,
        dark_pool_proxy_config=DarkPoolProxyConfig(
            us_min_notional=100_000.0,
            alert_cooldown_s=0.0,
        ),
        l2_imbalance_config=L2ImbalanceConfig(
            level=10,
            persist_snapshots=1,
            alert_cooldown_s=0.0,
        ),
    )
    writer.start()
    writer.enqueue(
        pd.DataFrame(
            [
                {
                    "code": "US.AAPL",
                    "name": "Apple",
                    "time": "2026-06-05 09:30:01",
                    "price": 200.0,
                    "volume": 600,
                    "turnover": 120_000.0,
                    "ticker_direction": "SELL",
                    "sequence": 10,
                    "type": "AUTO_MATCH",
                }
            ]
        ),
        "cache",
    )
    book = _book("US.AAPL", bid_size=100.0, ask_size=1000.0)
    snapshot_ts = pd.Timestamp("2026-06-05T13:30:02Z").timestamp()
    book["svr_recv_time_bid_timestamp"] = snapshot_ts
    book["svr_recv_time_ask_timestamp"] = snapshot_ts
    writer.enqueue_order_book(book, "cache")
    writer.stop()
    store.rebuild_microstructure_daily_features()
    store.finish_run("success")
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM realtime_ticks").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM dark_pool_proxy_events"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM dark_pool_proxy_metrics"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM l2_imbalance_signals"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM microstructure_alerts"
        ).fetchone()[0] == 2
        assert conn.execute(
            """
            SELECT dark_pool_event_count,
                   dark_pool_sell_notional,
                   l2_snapshot_count,
                   l2_danger_count
              FROM microstructure_daily_features
             WHERE trade_date = '2026-06-05' AND _code = 'US.AAPL'
            """
        ).fetchone() == (1, 120_000.0, 1, 1)
        assert conn.execute(
            """
            SELECT dark_pool_proxy_events,
                   dark_pool_proxy_metric_rows,
                   l2_imbalance_signal_rows,
                   microstructure_alerts,
                   microstructure_daily_feature_rows
              FROM tick_runs
            """
        ).fetchone() == (1, 1, 1, 2, 1)
    finally:
        conn.close()


def test_compute_order_book_metrics_spread_and_imbalance() -> None:
    metrics = compute_order_book_metrics(
        {
            "code": "US.AAPL",
            "Bid": [(10.0, 1000, 2, {})],
            "Ask": [(10.02, 500, 1, {})],
        },
        slippage_qty=100,
    )

    assert metrics["spread_bps"] == pytest.approx(19.98001998)
    assert metrics["imbalance_1"] == pytest.approx(1 / 3)
    assert metrics["micro_price"] == pytest.approx(10.0133333333)


def test_l2_imbalance_score_bid_and_ask_heavy() -> None:
    bid_heavy = compute_order_book_metrics(
        _book("US.AAPL", bid_size=1000.0, ask_size=100.0),
        levels=(10,),
    )
    ask_heavy = compute_order_book_metrics(
        _book("US.AAPL", bid_size=100.0, ask_size=1000.0),
        levels=(10,),
    )

    bid_signal = evaluate_l2_imbalance(
        bid_heavy, config=L2ImbalanceConfig(level=10)
    )
    ask_signal = evaluate_l2_imbalance(
        ask_heavy, config=L2ImbalanceConfig(level=10)
    )

    assert bid_signal.score < 50.0
    assert bid_signal.risk_level == "support"
    assert ask_signal.score > 70.0
    assert ask_signal.risk_level == "danger"


def test_l2_imbalance_tracker_alerts_after_persistent_pressure() -> None:
    tracker = L2ImbalanceTracker(
        L2ImbalanceConfig(
            level=10,
            persist_snapshots=2,
            alert_cooldown_s=999.0,
        )
    )
    book = _book("US.AAPL", bid_size=100.0, ask_size=1000.0)

    first = tracker.update(book)
    second = tracker.update(book)
    third = tracker.update(book)

    assert first is not None
    assert second is not None
    assert third is not None
    assert not first.should_alert
    assert second.should_alert
    assert not third.should_alert
    assert tracker.latest("US.AAPL") == third


def _book(code: str, bid_size: float, ask_size: float) -> dict:
    return {
        "code": code,
        "Bid": [(10.0 - i * 0.01, bid_size, 1, {}) for i in range(10)],
        "Ask": [(10.01 + i * 0.01, ask_size, 1, {}) for i in range(10)],
    }
