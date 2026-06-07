# -*- coding: utf-8 -*-
import logging
import threading
from typing import Callable

import moomoo as ft

from dark_pool_proxy import DarkPoolProxyTracker
from order_book_l2 import L2ImbalanceTracker, OrderBookCache

logger = logging.getLogger(__name__)

# 回调类型：(股票代码, 最新价) -> None
OnQuoteCallback = Callable[[str, float], None]
OnAlertCallback = Callable[[str, str], None]
OnMarketDateCallback = Callable[[], str]


class _QuotePushHandler(ft.StockQuoteHandlerBase):
    def __init__(self, callback: OnQuoteCallback):
        super().__init__()
        self._callback = callback

    def on_recv_rsp(self, rsp_pb):
        ret, df = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            logger.debug("行情推送解析失败: %s", df)
            return ft.RET_ERROR, df

        for _, row in df.iterrows():
            try:
                self._callback(str(row["code"]), float(row["last_price"]))
            except Exception as exc:
                logger.error("行情回调异常 %s: %s", row.get("code"), exc)
        return ft.RET_OK, df


class _OrderBookPushHandler(ft.OrderBookHandlerBase):
    """Update the in-process L2 order book cache from push snapshots."""

    def __init__(
        self,
        cache: OrderBookCache,
        tracker: L2ImbalanceTracker | None = None,
        alert_callback: OnAlertCallback | None = None,
    ) -> None:
        super().__init__()
        self._cache = cache
        self._tracker = tracker
        self._alert_callback = alert_callback

    def on_recv_rsp(self, rsp_pb) -> tuple[int, object]:
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            logger.debug("盘口推送解析失败: %s", data)
            return ft.RET_ERROR, data
        if isinstance(data, dict):
            self._cache.update(data)
            if self._tracker is not None:
                signal = self._tracker.update(data)
                if (
                    signal is not None
                    and signal.should_alert
                    and self._alert_callback is not None
                ):
                    self._alert_callback("L2盘口失衡", signal.alert_message())
        return ft.RET_OK, data


class _TickerPushHandler(ft.TickerHandlerBase):
    """Scan ticker push rows for moomoo large-print proxy alerts."""

    def __init__(
        self,
        tracker: DarkPoolProxyTracker,
        market_date_provider: OnMarketDateCallback,
        alert_callback: OnAlertCallback | None = None,
    ) -> None:
        super().__init__()
        self._tracker = tracker
        self._market_date_provider = market_date_provider
        self._alert_callback = alert_callback

    def on_recv_rsp(self, rsp_pb) -> tuple[int, object]:
        ret, df = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            logger.debug("逐笔推送解析失败: %s", df)
            return ft.RET_ERROR, df
        for metrics in self._tracker.update(
            df,
            market_date=self._market_date_provider(),
        ):
            if metrics.should_alert and self._alert_callback is not None:
                self._alert_callback("疑似暗池/大额逐笔", metrics.alert_message())
        return ft.RET_OK, df


class RealtimeMonitor:
    def __init__(
        self,
        quote_ctx: ft.OpenQuoteContext,
        callback: OnQuoteCallback,
        extra_sub_types: list | None = None,
        order_book_cache: OrderBookCache | None = None,
        l2_imbalance_tracker: L2ImbalanceTracker | None = None,
        l2_alert_callback: OnAlertCallback | None = None,
        dark_pool_proxy_tracker: DarkPoolProxyTracker | None = None,
        dark_pool_market_date_provider: OnMarketDateCallback | None = None,
        dark_pool_alert_callback: OnAlertCallback | None = None,
    ) -> None:
        self._ctx = quote_ctx
        self._callback = callback
        self._order_book_cache = order_book_cache
        self._l2_imbalance_tracker = l2_imbalance_tracker
        self._l2_alert_callback = l2_alert_callback
        self._dark_pool_proxy_tracker = dark_pool_proxy_tracker
        self._dark_pool_market_date_provider = dark_pool_market_date_provider
        self._dark_pool_alert_callback = dark_pool_alert_callback
        # QUOTE 必订；微观结构因子（CVD/OBI）启用时追加 TICKER/ORDER_BOOK
        self._sub_types = list(dict.fromkeys([ft.SubType.QUOTE, *(extra_sub_types or [])]))
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    def start(self) -> None:
        self._ctx.set_handler(_QuotePushHandler(self._callback))
        if self._order_book_cache is not None:
            self._ctx.set_handler(
                _OrderBookPushHandler(
                    self._order_book_cache,
                    self._l2_imbalance_tracker,
                    self._l2_alert_callback,
                )
            )
        if (
            self._dark_pool_proxy_tracker is not None
            and self._dark_pool_market_date_provider is not None
        ):
            self._ctx.set_handler(
                _TickerPushHandler(
                    self._dark_pool_proxy_tracker,
                    self._dark_pool_market_date_provider,
                    self._dark_pool_alert_callback,
                )
            )
        self._ctx.start()
        logger.info("实时行情监控已启动")

    def subscribe(self, codes: list[str]) -> None:
        new_codes = [c for c in codes if c not in self._subscribed]
        if not new_codes:
            return
        ret, msg = self._ctx.subscribe(new_codes, self._sub_types)
        if ret != ft.RET_OK:
            logger.error("订阅失败 %s: %s", new_codes, msg)
            return
        with self._lock:
            self._subscribed.update(new_codes)
        logger.info("已订阅 %d 只新股: %s", len(new_codes), new_codes)

    def unsubscribe(self, codes: list[str]) -> None:
        targets = [c for c in codes if c in self._subscribed]
        if not targets:
            return
        ret, msg = self._ctx.unsubscribe(targets, self._sub_types)
        if ret != ft.RET_OK:
            logger.error("取消订阅失败 %s: %s", targets, msg)
            return
        with self._lock:
            self._subscribed.difference_update(targets)
        logger.info("已取消订阅: %s", targets)

    def current_subscriptions(self) -> list[str]:
        with self._lock:
            return list(self._subscribed)

    def stop(self) -> None:
        self._ctx.stop()
        logger.info("实时行情监控已停止")
